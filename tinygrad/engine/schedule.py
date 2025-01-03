import sys, atexit, functools, pickle
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import FrozenSet, Set, Tuple, List, Dict, Optional, DefaultDict
from tinygrad.ops import GroupOp, UOp, Ops, PatternMatcher, UPat, Variable, can_pad, graph_rewrite, resolve, track_rewrites, view_left, merge_views
from tinygrad.ops import identity_element, buffers, exec_alu
from tinygrad.helpers import Context, Metadata, all_int, all_same, colored, diskcache_put, merge_dicts, prod, dedup, getenv, unwrap
from tinygrad.helpers import FUSE_CONV_BW, FUSE_ARANGE, DEBUG, ContextVar
from tinygrad.dtype import ConstType, ImageDType, dtypes
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.view import View, strides_for_shape
from tinygrad.device import Buffer

# creation can recurse a lot
sys.setrecursionlimit(10000)

BUF_LIMIT = {"METAL":32}

# **** ScheduleItem return type

@dataclass(frozen=True)
class ScheduleItem:
  ast: UOp
  bufs: Tuple[Buffer, ...]
  metadata: Tuple[Metadata, ...]
  assign_preloads: FrozenSet[UOp]
  @property
  def outputs(self) -> Tuple[Buffer, ...]:
    """Read/write or write only buffers in the schedule."""
    return tuple(b for i,b in enumerate(self.bufs) if i in self.output_idxs)
  @property
  def inputs(self) -> Tuple[Buffer, ...]:
    """Read only buffers in the schedule."""
    return tuple(b for i,b in enumerate(self.bufs) if i not in self.output_idxs)
  @functools.cached_property
  def output_idxs(self) -> Tuple[int, ...]: return tuple(x.src[0].arg for x in self.ast.src) if self.ast.op is Ops.SINK else (0,)

# **** Schedule context and big graph

@dataclass(frozen=True)
class ScheduleContext:
  lazybufs: Dict[UOp, List[UOp]] = field(default_factory=dict) # this maps BUFFER uops of this schedule to the underlying lazybuffer
  var_vals: Dict[Variable, int] = field(default_factory=dict)        # this maps a BIND's DEFINE_VAR to its value
  assigns: Set[UOp] = field(default_factory=set)                     # this holds all the BUFFER uops we ASSIGN to in this schedule
  realizes: Dict[UOp, UOp] = field(default_factory=dict)             # this holds all the BUFFER uops we mutate in this schedule
  allbufs: Dict[UOp, UOp] = field(default_factory=dict)              # this maps BUFFER uops the actual op
  ops_metadata: Dict[UOp, Metadata] = field(default_factory=dict)    # this maps fused ops to Metadata
  children: DefaultDict[UOp, Dict[UOp, None]] = field(default_factory=lambda: defaultdict(dict))
  contiguous: Dict[UOp, UOp] = field(default_factory=dict)           # this maps roots to places they are made contiguous

def is_scheduled(u:UOp) -> bool: return u.op is Ops.VIEW and len(u.src) == 2

def to_uop(buf:UOp, ctx:ScheduleContext, cache:Dict[UOp, UOp]) -> UOp:
  if (r:=cache.get(buf)) is not None: return r
  # shapeless op is passthrough
  if buf.st is None: return buf
  # view is passthrough
  if buf is not buf.base:
    cache[buf] = ret = to_uop(buf.base, ctx, cache).view(buf.st)
    return ret
  # make things that can't be images not images
  dtype = buf.buf_uop.dtype.base if buf.is_realized else buf.dtype
  if isinstance(dtype, ImageDType) and (prod(buf.shape) != prod(dtype.shape) or not any(buf.shape[x]%4 == 0 for x in buf.st.unit_stride_axes())):
    assert buf.realized is None, "can't fixup allocated buffer"
    if DEBUG >= 2: print(f"forcing image {dtype} with shape {buf.shape} to {dtype.base}")
    dtype = buf.dtype.base
  # base is a VIEW of (BUFFER, (optional) op)
  if buf.is_realized:
    buf_uop = buf.buf_uop
    op = None
  # metaops already have a BUFFER uop
  elif is_scheduled(buf):
    buf_uop = buf.buf_uop
    op = buf.src[1].replace(src=tuple(to_uop(x, ctx, cache) for x in buf.src[1].src))
  # ASSIGN uses the target buffer, otherwise we create a new buffer
  else:
    src = tuple(to_uop(x, ctx, cache) for x in buf.srcs)
    buf_uop = src[0].base.buf_uop if buf.op is Ops.ASSIGN else UOp.new_buffer(buf.device, buf.size, dtype)
    op = UOp(buf.op, dtype.base, src, buf.arg)
  ret = UOp(Ops.VIEW, dtype.base, (buf_uop,) if op is None else (buf_uop, op.alu(Ops.CONTIGUOUS) if buf.forced_realize else op), buf.st)
  # track the underlying lazydata for this op
  if op is not None: ctx.lazybufs[buf_uop] = [buf]
  cache[buf] = ret
  return ret

# **** AST graph rewrite

# ** movement ops

def apply_swizzle(u:UOp, arg:ShapeTracker) -> UOp:
  assert u is u.base, f"must be base to swizzle {u}"
  with Context(TRACK_MATCH_STATS=0): return graph_rewrite(u.view(arg), view_left)

def swizzle_r(r:UOp, src:UOp, st:ShapeTracker) -> UOp:
  input_st = ShapeTracker.from_shape(unwrap(src.st).shape)
  tmp = input_st.permute(tuple(i for i in range(len(input_st.shape)) if i not in r.axis_arg)+r.axis_arg)
  prshape = prod(rshape:=tmp.shape[-len(r.axis_arg):])
  strides = strides_for_shape(rshape)
  nv = [View.create(v.shape+rshape, tuple(x*prshape for x in v.strides)+strides,
                    v.offset*prshape, v.mask+tuple((0,s) for s in rshape) if v.mask is not None else None) for v in st.views]
  # update input_st and axis
  new_input_st = tmp + ShapeTracker(tuple(nv))
  new_axis = tuple(range(len(st.shape), len(st.shape) + len(r.axis_arg)))
  return apply_swizzle(src, new_input_st).r(r.arg[0], new_axis).view(ShapeTracker.from_shape(st.shape))

def push_swizzle_down_through_reduce(r:UOp, v:UOp, src:UOp) -> UOp:
  if not (swizzle_st:=unwrap(v.st)).contiguous or v.size != src.size: raise AssertionError(f"can't push {v} down through {src}")
  output_shape = swizzle_st.reduce(r.axis_arg)
  return src.r(r.arg[0], tuple(i for i,(s,u) in enumerate(zip(src.shape, output_shape)) if s != u)).view(ShapeTracker.from_shape(output_shape))

def push_swizzle_down_through_elementwise(root:UOp) -> Optional[UOp]:
  if not (swizzles := [x for x in root.src if x.base is not x]): return None
  assert all_same([(x.shape, prod(x.src[0].shape)) for x in swizzles]), f"swizzles must have the same size {swizzles}"
  new_input_st = ShapeTracker.from_shape(swizzles[0].src[0].shape)
  ret = root.replace(src=tuple(x if not x.has_st else x.src[0] if x in swizzles else apply_swizzle(x, new_input_st) for x in root.src))
  # update the ASSIGN offset to match the new shape
  if ret.op is Ops.ASSIGN and ret.arg is not None: ret = ret.replace(arg=ret.arg+new_input_st,)
  return ret if ret.op is Ops.STORE else ret.view(ShapeTracker.from_shape(swizzles[0].shape))

def merge_double_reduce(root:UOp, first_reduce:UOp) -> UOp:
  assert root.arg[0] == first_reduce.arg[0], "can't merge reduceops with different alu"
  assert not any(x.op is Ops.REDUCE_AXIS for x in first_reduce.src[0].toposort), "can't merge more than two reduceops at a time"
  return first_reduce.src[0].r(first_reduce.arg[0], root.axis_arg+first_reduce.axis_arg)

# push VIEW to stores
view_right = merge_views+PatternMatcher([
  # ASSIGN with offset swizzles STORE
  (UPat(Ops.STORE, src=(UPat.var("b"), UPat.var("st"), UPat(Ops.ASSIGN, name="a"))),
   lambda a,b,st: None if a.arg is None else apply_swizzle(UOp.store(b, st, a.replace(arg=None)), a.arg)),
  # non contiguous VIEW on a reduce creates a new VIEW
  (UPat(Ops.REDUCE_AXIS, src=(UPat.var("src"),), name="r").view(name="v"), lambda v,r,src: None if v.st.contiguous else swizzle_r(r, src, v.st)),
  # push a VIEW down to STORE, through a reduce (ONLY reshapes)
  (UPat(Ops.REDUCE_AXIS, src=(UPat.var("src").view(name="v"),), name="r"), push_swizzle_down_through_reduce),
  # push VIEW(s) down to STORE, through an elementwise op (ONLY reshapes)
  (UPat((*GroupOp.ALU, Ops.CAST, Ops.BITCAST, Ops.ASSIGN, Ops.CONTIGUOUS, Ops.STORE), name="root"), push_swizzle_down_through_elementwise),
  (UPat(Ops.REDUCE_AXIS, src=(UPat(Ops.REDUCE_AXIS, name="first_reduce"),), name="root"), merge_double_reduce),
])

# ** ScheduleItem context builder

@dataclass(frozen=True)
class ScheduleItemContext:
  lazybufs: Dict[UOp, List[UOp]]
  ops_metadata: Dict[UOp, Metadata]
  assigns: Set[UOp]
  var_vals: Dict[Variable, int]
  sinked: Dict[UOp, UOp]
  sts: Set[ShapeTracker] = field(default_factory=set)
  bufs: List[UOp] = field(default_factory=list)
  metadata: Set[Metadata] = field(default_factory=set)
  assign_adj: Dict[UOp, List[UOp]] = field(default_factory=dict)

def _append_st_vars(ctx:ScheduleItemContext, x:UOp) -> Optional[UOp]:
  if (st:=unwrap(x.st)) in ctx.sts: return None
  st, var_vals = st.simplify().unbind()
  ctx.var_vals.update(var_vals)
  ctx.sts.add(st)
  return st.to_uop() if st != x.st else None

def _append_buf(ctx:ScheduleItemContext, x:UOp) -> UOp:
  ctx.bufs.append(x)
  return UOp(Ops.DEFINE_GLOBAL, x.dtype, (), len(ctx.bufs)-1)
append_bufs = PatternMatcher([(UPat(Ops.BUFFER, name="x"), _append_buf)])

def _append_preload(ctx:ScheduleItemContext, x:UOp, b:UOp) -> UOp:
  (adj_loads:=ctx.assign_adj.setdefault(b, [])).append(x)
  if not all_same([x.op for x in adj_loads]): raise RuntimeError(f"Detected cycle when fusing {adj_loads}. Can only fuse PRELOAD or LOAD of {b}")
  return x.replace(op=Ops.LOAD)
check_preload = PatternMatcher([(UPat(Ops.PRELOAD, src=(UPat.var("b"), UPat()), name="x"), _append_preload),])

to_si = PatternMatcher([
  (UPat(Ops.VIEW, name="x"), _append_st_vars),
  (UPat(Ops.SINK, src=(UPat.store(UPat.var("b"), UPat(), UPat(GroupOp.Meta, name="x")),)), lambda ctx,b,x: x.replace(src=(b, *x.src))),
  # don't need contiguous or assign anymore
  (UPat(Ops.CONTIGUOUS, src=(UPat.var("x"),)), lambda ctx,x: x),
  (UPat(Ops.ASSIGN, src=(UPat(), UPat.var("x"),)), lambda ctx,x: x),
])

# ** fusion

lazy = PatternMatcher([
  # gather the metadata for this kernel
  (UPat(tuple(Ops), name="x"), lambda ctx,x: ctx.metadata.add(m) if (m:=ctx.ops_metadata.get(x)) is not None else None),
])

multioutput = PatternMatcher([(UPat.load(UPat.var("b"), UPat()), lambda ctx,b: ctx.sinked.get(b)),])

append_load = PatternMatcher([(UPat.load(UPat.var("b"), UPat(), name="x"), lambda ctx,b,x: ctx.assign_adj.setdefault(b, []).append(x)
                               if b in ctx.assigns else None)])

def full_ast_rewrite(pre:UOp, ctx:ScheduleContext) -> Tuple[UOp, ScheduleItemContext]:
  si_ctx = ScheduleItemContext(ctx.lazybufs, ctx.ops_metadata, ctx.assigns, ctx.var_vals, {x.buf_uop:x.src[2] for x in pre.src},
                               # TODO: fix metadata merging, this should be a list
                               metadata={m for x in pre.src if (l:=ctx.lazybufs.get(x.buf_uop)) is not None and (m:=l[0].metadata) is not None})
  # fuse and fold store -> loads
  ops_folding = lazy if len(si_ctx.sinked) == 1 else lazy+multioutput
  sink = graph_rewrite(pre, ops_folding if len(si_ctx.assigns) == 0 else ops_folding+append_load, si_ctx)
  # do movement ops
  sink = graph_rewrite(graph_rewrite(sink, view_left), view_right)
  # convert to AST
  sink = graph_rewrite(graph_rewrite(sink, to_si+check_preload if len(si_ctx.assigns) != 0 else to_si, si_ctx), append_bufs, si_ctx)
  # assert buffer count limit
  if (limit:=BUF_LIMIT.get(device:=si_ctx.bufs[0].device)) is not None and len(si_ctx.bufs) >= limit:
    if DEBUG >= 3: print(sink)
    raise RuntimeError(f"Kernel for {si_ctx.metadata} exceeded the {limit} buffer count limit for {device} with {len(si_ctx.bufs)} buffers.")
  # we also allow masked views. if it has a single view and it's equal when you shrink a contig, it's fine
  for ubuf,ops in si_ctx.assign_adj.items():
    if si_ctx.sinked.get(ubuf) is not None and not all((s:=x.st_arg).contiguous or (len(s.views) == 1 and (m:=s.views[0].mask) is not None \
        and ShapeTracker.from_shape(s.shape).shrink(m) == s.shrink(m)) for x in ops):
      raise RuntimeError("self operand of augmented assign must be contiguous.\nhelp: consider using .contiguous():\n"
                         +colored("   - a += a.T\n", "red")+colored("   + a += a.T.contiguous()", "green"))
  if getenv("RUN_PROCESS_REPLAY"):
    PROCESS_REPLAY_CAPTURE[str(pre.key)] = pickle.dumps((pre, si_ctx.assigns, {k:v.value for k,v in ContextVar._cache.items()}, sink))
  return sink, si_ctx

PROCESS_REPLAY_CAPTURE: Dict[str, bytes] = {}
if getenv("RUN_PROCESS_REPLAY"):
  @atexit.register
  def save_process_replay() -> None:
    for k,v in PROCESS_REPLAY_CAPTURE.items(): diskcache_put("schedule_process_replay", k, v, prepickled=True)

# **** Schedule grouping

def uval(u:UOp) -> UOp:
  assert is_scheduled(u), f"must be a scheduled op {u}"
  return r.src[0] if (r:=u.src[1]).op is Ops.CONTIGUOUS and not (r.src[0].base.op is Ops.VIEW and len(r.src[0].base.src) == 2) else r

def recursive_group(tr:UOp, st:ShapeTracker, r:UOp, children:DefaultDict[UOp, Dict[UOp, None]], allbufs:Dict[UOp, UOp], realizes:Dict[UOp, UOp],
                     reduce_for_op:Dict[UOp, UOp], group:Dict[UOp, None], cache:Dict[Tuple[UOp, ShapeTracker], None]) -> None:
  """recursively search the uop for groupable children, realize the UOp if a child can't group"""
  if (tr, st) in cache: return
  cache.setdefault((tr, st))
  rsize = unwrap(allbufs[r].st).size
  if tr in realizes and tr is not r:
    # can only fuse contiguous
    # max one reduceop per kernel
    if not st.contiguous or st.size != rsize or tr in reduce_for_op: group.setdefault(r)
    return group.setdefault(tr)
  for tr_next in children[tr]:
    # max one reduceop per kernel
    if (tr_next_uop:=uval(allbufs[tr_next]).base).op is Ops.REDUCE_AXIS: return group.setdefault(r)
    # can only fuse contiguous
    if len(st_childs:=dedup(unwrap(x.st) for x in tr_next_uop.src if is_scheduled(x.base) and x.base.buf_uop == tr)) > 1: return group.setdefault(r)
    recursive_group(tr_next, st+st_childs[0], r, children, allbufs, realizes, reduce_for_op, group, cache)

def get_isolated_children(r:UOp, reduce_for_op:Dict[UOp, UOp], children:DefaultDict[UOp, Dict[UOp, None]], allbufs:Dict[UOp, UOp],
                           realizes:Dict[UOp, UOp], group:Dict[UOp, None]) -> Dict[UOp, None]:
  rc_parents, cache = deque(group), set()
  while rc_parents:
    if (p:=uval(allbufs[rc_parents.pop()])) in cache: continue
    cache.add(p)
    # max one reduceop per kernel
    if p.op is Ops.REDUCE_AXIS: return {}
    rc_parents.extend(x.base.buf_uop for x in p.src if is_scheduled(x.base) and x.base.buf_uop is not r)
  # search descendants of the reduceop that can cleanly group
  descendants: Dict[UOp, None] = {}
  for tr in group: recursive_group(tr, unwrap(allbufs[tr].st), tr, children, allbufs, realizes, reduce_for_op, descendants, cache={})
  return merge_dicts([group, {} if any(tr in group for tr in descendants) else descendants])

def group_realizes(ctx:ScheduleContext) -> List[List[UOp]]:
  """search the big graph for all the reduceops that need to realize, sometimes group/fuse the reduceop"""
  # find all reduces, and pair them to a elementwise op. if they can't be cleanly paired, force realize the reduce (or a contig child)
  reduce_for_op: Dict[UOp, UOp] = {}
  reduce_of_const: List[UOp] = []
  double_reduces: List[UOp] = []
  for r, r_uop in ctx.allbufs.items():
    if (r_uop:=uval(r_uop)).op is not Ops.REDUCE_AXIS: continue
    if FUSE_CONV_BW and uval((x:=r_uop.src[0]).base).op is r_uop.op and x.base is not x: double_reduces.append(r)
    if r in ctx.realizes: continue
    group: Dict[UOp, None] = {}
    recursive_group(r, unwrap(r_uop.st), r, ctx.children, ctx.allbufs, ctx.realizes, reduce_for_op, group, cache={})
    # max one reduceop per kernel
    can_chase = all(tr not in reduce_for_op for tr in group)
    # TODO: forced_realize exists because the scheduler is incapable of checking for self-contained DAGs
    forced_realize = r in group
    if not forced_realize and len(group) > 1:
      group = get_isolated_children(r, reduce_for_op, ctx.children, ctx.allbufs, ctx.realizes, group)
    # can only fuse assign if no other assign_target is used in the kernel
    if not forced_realize and any(x in ctx.assigns for x in group):
      parents = deque((r, *group))
      while parents and not forced_realize:
        if (p_uop:=ctx.allbufs.get(p:=parents.pop())) is None: continue
        if (p_uop:=uval(p_uop)).op is Ops.ASSIGN and p not in group: forced_realize, can_chase = True, False
        if p in ctx.realizes: continue
        parents.extend([x.base.src[0] for x in p_uop.src if x.base.op is Ops.VIEW and len(x.base.src) != 0])
    if forced_realize or not group:
      tr = r
      if can_chase:
        # can chase this down to contiguous children
        st = unwrap(r_uop.st)
        while len(ctx.children[tr]) == 1:
          tr_next_uop = uval(ctx.allbufs[(tr_next:=next(iter(ctx.children[tr])))])
          st_childs = dedup([unwrap(x.st) for x in tr_next_uop.src if is_scheduled(x.base) and x.base.buf_uop is tr])
          if len(st_childs) > 1: break
          if st.size != st_childs[0].size: break
          st = st + st_childs[0]
          if not st.contiguous or tr_next_uop.op is Ops.REDUCE_AXIS: break
          tr = tr_next
        # don't cast to higher size before store (tr cannot be realized if forced_realize)
        if (tr_uop:=uval(ctx.allbufs[tr])).op is Ops.CAST and tr_uop.dtype.base.itemsize > tr_uop.src[0].dtype.base.itemsize:
          tr = tr_uop.src[0].base.buf_uop
      group = {tr: None}
      ctx.realizes[tr] = tr
    reduce_for_op.update((tr, r) for tr in group)
    if FUSE_ARANGE and r_uop.arg[0] is Ops.ADD and uval(r_uop.src[0].base).op is Ops.CONST: reduce_of_const.append(r)
  # fuse double reduces with no other child
  for reduceop in double_reduces:
    top_reduce = uval(ctx.allbufs[reduceop]).src[0].base.buf_uop
    if len(ctx.children[top_reduce]) == 1: del ctx.realizes[top_reduce]
  # maybe fuse arange with its children
  for rbuf in reduce_of_const:
    group = {tr:None for tr,rop in reduce_for_op.items() if rop is rbuf}
    if any(luop.forced_realize for tr in group for luop in ctx.lazybufs[tr]): continue
    kernel_children = {c for tr in group for c in ctx.children[tr] if uval(ctx.allbufs[c]).op not in {Ops.COPY, Ops.BUFFER_VIEW}}
    if len(kernel_children) == 0: continue
    for tr in group: del ctx.realizes[tr]
  # group BUFFER uops into kernels
  output_groups: DefaultDict[UOp, List[UOp]] = defaultdict(list)
  for ubuf in ctx.realizes: output_groups[reduce_for_op.get(ubuf, ubuf)].append(ubuf)
  return list(output_groups.values())

# **** Schedule creation and BFS toposort

# ** ops in the big graph can either be pre-realized or scheduled (fused/realized)

class UPatScheduled(UPat):
  def __init__(self, *args, **kwargs): super().__init__(Ops.VIEW, name="base", src=(UPat(Ops.BUFFER, name="b"),
                                                                       UPat(*args, **{"name":"to_store",**kwargs})))

# ** this is schedule level const folding

def _as_const(u:UOp, val:ConstType) -> UOp:
  assert is_scheduled(u), f"must be scheduled to fold {u}"
  st = (base:=ShapeTracker.from_shape(())).reshape((1,)*len(u.shape)).expand(u.shape)
  return UOp(Ops.VIEW, u.dtype, (u.buf_uop, UOp.const(u.dtype, val)), base).view(st)

def simplify_reduceop(ctx, reduce:UOp, x:UOp) -> Optional[UOp]:
  # remove reduce on unmasked const
  if all_int(x.shape) and x.is_unrealized_unmasked_const():
    prshape = prod(unwrap(x.st).shape[i] for i in reduce.arg[1])
    ret = x.const_arg
    match reduce.arg[0]:
      case Ops.ADD: ret *= prshape
      case Ops.MUL: ret **= prshape
      case Ops.MAX: pass # NOTE: Ops.MAX is passthrough
      case _: return None
    return UOp.const(reduce.dtype, ret)
  return None

def simplify_alu(alu:UOp):
  if not all(x.is_unrealized_unmasked_const() for x in alu.src): return None
  # this needs to have a VIEW next (it has to, right?)
  return UOp.const(alu.dtype, exec_alu(alu.op, alu.dtype, [s.const_arg for s in alu.src]))

def simplify_binop(binop:UOp, x:UOp, y:UOp):
  if all_int(x.shape) and x.is_unrealized_unmasked_const(): other, const = y, x
  elif all_int(y.shape) and y.is_unrealized_unmasked_const():
    if binop.op is Ops.IDIV and y.const_arg == 1: return x
    other, const = x, y
  else: return None
  if binop.op is Ops.ADD and const.const_arg == 0: return other
  if binop.op is Ops.MUL and const.const_arg == 1: return other
  if binop.op is Ops.MUL and const.const_arg == 0: return UOp.const(binop.dtype, 0)

def found_contiguous(ctx:ScheduleContext, contig:UOp, base:UOp, b:UOp):
  if contig.src[0].op is Ops.VIEW and len(contig.src[0].src):
    old_base = contig.src[0].src[0]
    if old_base.op is Ops.VIEW and (sti:=unwrap(contig.src[0].st).invert(old_base.shape)) is not None: ctx.contiguous[old_base] = base.view(sti)
def replace_contiguous(ctx:ScheduleContext, alu:UOp):
  new_src = list(alu.src)
  for i,s in enumerate(alu.src):
    if (replace_src:=ctx.contiguous.get(s, None)) is not None: new_src[i] = replace_src
  if tuple(new_src) != alu.src: return alu.replace(src=tuple(new_src))

ops_folding = PatternMatcher([
  # op with size 0 is zero
  (UPatScheduled(), lambda b,to_store,base: _as_const(base, 0) if base.size == 0 else None),
  # elementwise const folding
  (UPat(GroupOp.ALU, name="alu"), simplify_alu),
  (UPat({Ops.ADD, Ops.MUL, Ops.IDIV}, name="binop", src=(UPat.var("x"), UPat.var("y"))), simplify_binop),
  (UPat(Ops.CAST, src=(UPat.var("x"),), name="cast"),
   lambda x,cast: UOp.const(cast.dtype, x.const_arg) if all_int(x.shape) and x.is_unrealized_unmasked_const() else None),
  # reduce of size 0 is the identity element
  (UPat(Ops.REDUCE_AXIS, name="reduce", src=(UPat.var("x"),)),
   lambda reduce,x:UOp.const(reduce.dtype, identity_element(reduce.arg[0], reduce.dtype)) if x.size == 0 and reduce.size != 0 else None),
  # reduce of const is collapsed (TODO: make this a generic rule for stride0)
  (UPat(Ops.REDUCE_AXIS, name="reduce", src=(UPat.var("x"),)), simplify_reduceop),
  # CONST doesn't need COPY
  (UPat(Ops.COPY, src=(UPat.var("x"),)), lambda x:x if x.is_unrealized_const() else None),
  # no double COPY
  (UPat(Ops.COPY, src=(UPat(Ops.VIEW, src=(UPat(), UPat(Ops.COPY, name="base")),))), lambda base: base),
  # no COPY to same device, except clone (arg is True)
  (UPatScheduled(Ops.COPY, src=UPat(Ops.VIEW, name="copyin"), name="copy"),
   lambda base,b,copyin,copy: copyin if base.device == copy.device and copy.arg is not True else None),
  # support for using a contiguous permuted view instead of the parent view if one exists
  (UPatScheduled(Ops.CONTIGUOUS, name="contig"), found_contiguous),
  (UPat(GroupOp.ALU, name="alu"), replace_contiguous),
])

# ** buffer merging

def merge(ctx:ScheduleContext, v1:UOp, b1:UOp, v2:UOp, b2:UOp) -> UOp:
  assert v1.st is not None and v2.st is not None and v1.st == v2.st, f"implicit movementop {v1.st} {v2.st}"
  # if b2 is realized also realize b1
  if b2 in ctx.realizes:
    ctx.realizes[b1] = b1
    del ctx.realizes[b2]
  # ops referring to b2 now ref to b1
  ctx.lazybufs[b1] += ctx.lazybufs[b2]
  del ctx.lazybufs[b2]
  # merge
  return v1

def merge_realized(ctx:ScheduleContext, v1:UOp, b1:UOp, v2:UOp, b2:UOp):
  # early become
  for luop in ctx.lazybufs.get(b1, [])+ctx.lazybufs.get(b2, []): luop.become(b1.view(unwrap(luop.st)))
  return v1

merge_bufs = PatternMatcher([
  # merge base
  (UPat(Ops.VIEW, name="v2", src=(UPat(Ops.BUFFER, name="b2"), UPat(Ops.VIEW, name="v1", src=(UPat(Ops.BUFFER, name="b1"), UPat())))), merge),
  (UPat(Ops.VIEW, name="v2", src=(UPat(Ops.BUFFER, name="b2"), UPat(Ops.VIEW, name="v1", src=(UPat(Ops.BUFFER, name="b1"),)))), merge_realized),
  # collapse view
  (UPat(Ops.VIEW, src=(UPat(Ops.BUFFER), UPat(Ops.VIEW, src=(UPat(Ops.BUFFER), UPat())).view(name="mv"))), lambda mv:mv),
  (UPat(Ops.VIEW, src=(UPat(Ops.BUFFER), UPat(Ops.VIEW, src=(UPat(Ops.BUFFER),)).view(name="mv"))), lambda mv:mv),
  (UPat(Ops.VIEW, src=(UPat(Ops.BUFFER), UPat(Ops.BUFFER).view(name="mv"))), lambda mv:mv),
])

# ** this decides which ops get realized

def realize(ctx:ScheduleContext, b:UOp, to_store:UOp, **kwargs) -> None:
  if to_store.op not in {Ops.CONST, Ops.BIND}: ctx.realizes.update([(b, to_store)])

def realize_view(ctx:ScheduleContext, view:UOp, src:UOp, b:UOp, **kwargs) -> None:
  if src.st is None: return None
  st = unwrap(view.st)
  # fold simple pads
  if len(st.views) == 1 and (m:=st.views[-1].mask) is not None and all_int(src.shape) and resolve(prod(src.shape) >= prod([y-x for x,y in m])):
    return None if can_pad(src, ctx.realizes, set()) else realize(ctx, b, src)
  # early realize before expand
  if resolve(prod(src.shape) < prod(st.shape)): return realize(ctx, b, src)
  # otherwise safety check pads
  return None if (all(v.mask is None for v in st.views) or can_pad(src, ctx.realizes, set())) else realize(ctx, b, src)

def fold_img_cast(ctx:ScheduleContext, xb:UOp, view:UOp, b:UOp, to_cast:UOp, **kwargs) -> Optional[UOp]:
  if not isinstance(xb.dtype, ImageDType) or b not in ctx.realizes or xb not in ctx.realizes or uval(to_cast).op in GroupOp.Meta: return None
  del ctx.realizes[b]
  return to_cast.view(unwrap(view.st))

def init_big_graph(sink:UOp) -> Optional[UOp]:
  new_src = tuple(x.base for x in sink.src if is_scheduled(x.base) and x.base.src[1].op is not Ops.CONST)
  return None if new_src == sink.src else UOp(Ops.NOOP) if len(new_src) == 0 else UOp.sink(*new_src)

do_realize = PatternMatcher([
  # always realize sinked ops
  (UPat(Ops.SINK, name="sink"), init_big_graph),
  # always realize meta ops
  (UPatScheduled({Ops.ASSIGN, Ops.CONTIGUOUS, *GroupOp.Meta}), realize),
  # realize before expand or unsafe pad ops
  (UPatScheduled(name="src").view(name="view"), realize_view),
  # don't realize image to image casts
  (UPatScheduled(Ops.CAST, src=(UPat(Ops.VIEW, src=(UPat.var("xb"), UPat()), name="to_cast"),), dtype=dtypes.float).view(name="view"), fold_img_cast),
  # realize before COPY or BUFFER_VIEW
  (UPat((Ops.COPY, Ops.BUFFER_VIEW), src=(UPat.any(UPatScheduled(), UPatScheduled().view()),)), realize),
  # ASSIGN only needs the buffer
  (UPat(Ops.ASSIGN, src=(UPat(Ops.VIEW, name="dest"), UPat.var("src")), name="x"), lambda dest,src,x: x.replace(src=(dest.base.buf_uop, src))),
])

# ** this breaks down realized ops into STOREs and rewrites the ops to LOADs

def generate_valid(ctx:ScheduleContext, b:UOp, to_store:UOp, base:UOp) -> UOp:
  if to_store.op is Ops.BIND: ctx.var_vals.update([to_store.unbind()])
  return UOp.const_with_shape(base.dtype, to_store if to_store.op is Ops.BIND else to_store.arg, unwrap(base.st).shape)

def append_realize(ctx:ScheduleContext, b:UOp, to_store:UOp, base:UOp) -> UOp:
  ctx.realizes[b] = UOp.store(b, ShapeTracker.from_shape(base.shape).to_uop(), append_op(ctx, b, to_store))
  return UOp(Ops.LOAD, base.dtype, (b, unwrap(base.st).to_uop()))

def append_op(ctx:ScheduleContext, b:UOp, to_store:UOp) -> UOp:
  # TODO: metadata post merge
  if (m:=ctx.lazybufs[b][0].metadata) is not None: ctx.ops_metadata[to_store] = m
  return to_store

break_sched = PatternMatcher([
  # consts are always fused and generated
  (UPatScheduled({Ops.CONST, Ops.BIND}), generate_valid),
  # view of realized buffer just loads
  (UPat(Ops.BUFFER, name="b").view(name="v"), lambda ctx,b,v: UOp(Ops.PRELOAD if b in ctx.assigns else Ops.LOAD, b.dtype.base, (b, v.st.to_uop()))),
  # all other views either fold or realize with a store
  (UPatScheduled(), lambda ctx,b,to_store,base: append_realize(ctx, b, to_store, base) if b in ctx.realizes else append_op(ctx, b, to_store)),
])

# **** Schedule context builder

def append_uop(ctx:ScheduleContext, view:UOp, buf_uop:UOp) -> None:
  ctx.allbufs[buf_uop] = view
  if (op:=uval(view)).op is Ops.ASSIGN: ctx.assigns.add(buf_uop)
  for x in op.src:
    if is_scheduled(x.base): ctx.children.setdefault(x.base.buf_uop, {})[buf_uop] = None
  # BUFFER_VIEW overrides the underlying buffer
  if op.op is Ops.BUFFER_VIEW:
    buffers[buf_uop] = (x:=op.src[0]).base.buffer.view(view.size, view.dtype, unwrap(x.st).views[0].offset*x.dtype.itemsize)
  buf_uop.buffer.ref(1)
create_ctx = PatternMatcher([(UPat(Ops.VIEW, name="view", src=(UPat(Ops.BUFFER, name="buf_uop"), UPat())), append_uop)])

@track_rewrites(named=True)
def create_schedule_with_vars(outs:List[UOp]) -> Tuple[List[ScheduleItem], Dict[Variable, int]]:
  if len(outs:=dedup(x.base for x in outs if x.base.realized is None and x.base.op is not Ops.CONST)) == 0: return [], {}
  # create the big graph
  ctx = ScheduleContext()
  cache: Dict[UOp, UOp] = {}
  for u in (big_graph:=UOp.sink(*(to_uop(x, ctx, cache) for x in outs))).src: ctx.realizes[u.buf_uop] = u
  big_graph = graph_rewrite(big_graph, ops_folding+do_realize, ctx)
  big_graph = graph_rewrite(big_graph, merge_bufs, ctx)
  # create the scheduler context
  graph_rewrite(big_graph, create_ctx, ctx)
  # group realizes into kernels
  store_groups = group_realizes(ctx)
  graph_rewrite(big_graph, break_sched, ctx)
  # preschedule realize groups
  prescheduled: List[ScheduleItem] = []
  for store_uops in store_groups:
    if len(stores:=[ctx.realizes[u] for u in store_uops if ctx.realizes[u].op is Ops.STORE]) != 0:
      ast, ast_ctx = full_ast_rewrite(UOp.sink(*stores), ctx)
      prescheduled.append(ScheduleItem(ast, tuple(u.buffer for u in ast_ctx.bufs if u.size != 0), tuple(ast_ctx.metadata),
                                       frozenset(ubuf for ubuf,ops in ast_ctx.assign_adj.items() if any(x.op is Ops.PRELOAD for x in ops))))
      for buf_uop in ast_ctx.sinked:
        for luop in ast_ctx.lazybufs[buf_uop]: luop.become(buf_uop.view(unwrap(luop.st)))
  # do BFS
  schedule_targets = {out:si for si in prescheduled for out in si.outputs}
  graph: DefaultDict[ScheduleItem, List[ScheduleItem]] = defaultdict(list)
  in_degree: DefaultDict[ScheduleItem, int] = defaultdict(int)
  for si in prescheduled:
    # realize outputs before a parent is assigned to
    parents_assigns = dedup(xsi for x in si.assign_preloads if (xsi:=schedule_targets.get(x.buffer)) and xsi is not si)
    for assign in parents_assigns:
      graph[si].append(assign)
      in_degree[assign] += 1
    # realize outputs after all parents are realized
    scheduled_parents = dedup(xsi for x in si.inputs if (xsi:=schedule_targets.get(x)) is not None and xsi not in parents_assigns)
    for x in scheduled_parents:
      graph[x].append(si)
      in_degree[si] += 1
  queue = deque(si for si in prescheduled if in_degree[si] == 0)
  schedule: List[ScheduleItem] = []
  while queue:
    schedule.append(si:=queue.popleft())
    for x in graph[si]:
      in_degree[x] -= 1
      if in_degree[x] == 0: queue.append(x)
  # confirm everything was scheduled correctly
  if len(schedule) != (groups:=len(prescheduled)): raise RuntimeError(f"cycle detected in graph, grouped {groups} but only scheduled {len(schedule)}")
  if DEBUG >= 1 and len(schedule) >= 10: print(f"scheduled {len(schedule)} kernels")
  return schedule, ctx.var_vals

def create_schedule(outs:List[UOp]) -> List[ScheduleItem]:
  schedule, var_vals = create_schedule_with_vars(outs)
  assert len(var_vals) == 0
  return schedule
