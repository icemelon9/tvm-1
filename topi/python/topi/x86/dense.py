# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name,too-many-locals,unused-variable
"""x86 dense operators"""
from __future__ import absolute_import as _abs
import tvm
from tvm import autotvm
from tvm.autotvm.task.space import SplitEntity
from tvm.contrib import cblas

from .util import get_fp32_len
from .. import generic, tag
from ..util import traverse_inline, get_const_tuple

def _schedule_dense_pack_template(cfg, s, C):
    A, packedB = s[C].op.input_tensors

    CC = s.cache_write(C, "global")
    y, x = s[C].op.axis
    k, = s[CC].op.reduce_axis

    yt, yo, yi = cfg["tile_y"].apply(s, C, y)
    xt, xo, xi = cfg["tile_x"].apply(s, C, x)
    s[C].reorder(yt, xt, yo, xo, yi, xi)
    xyt = s[C].fuse(yt, xt)
    s[C].parallel(xyt)
    xyo = s[C].fuse(yo, xo)
    s[C].unroll(yi)
    s[C].vectorize(xi)

    s[CC].compute_at(s[C], xyo)
    y, x = s[CC].op.axis
    ko, ki = cfg["tile_k"].apply(s, CC, k)
    s[CC].reorder(ko, ki, y, x)
    s[CC].vectorize(x)
    s[CC].unroll(y)
    s[CC].unroll(ki)

    z, y, x = s[packedB].op.axis
    s[packedB].reorder(z, x, y)
    s[packedB].parallel(z)
    s[packedB].vectorize(y)
    return s


def _schedule_dense_nopack_template(cfg, s, C):
    y, x = s[C].op.axis
    kk, = s[C].op.reduce_axis
    yo, yi = cfg["tile_y"].apply(s, C, y)
    xo, xi = cfg["tile_x"].apply(s, C, x)
    s[C].reorder(yo, xo, yi, xi)
    xyo = s[C].fuse(yo, xo)
    s[C].parallel(xyo)
    s[C].unroll(kk)

    CC, = s[C].op.input_tensors
    s[CC].compute_at(s[C], xyo)
    z, y, x = s[CC].op.axis
    k, = s[CC].op.reduce_axis
    yz = s[CC].fuse(z, y)
    s[CC].reorder(k, yz, x)
    s[CC].unroll(yz)
    s[CC].vectorize(x)
    return s


def _default_dense_pack_config(cfg, M, N, K):
    # Generate default schedule for dynamic shape.
    if isinstance(M, tvm.expr.Var):
        M = 16
    if isinstance(N, tvm.expr.Var):
        N = 16
    if isinstance(K, tvm.expr.Var):
        K = 16

    vec_width = get_fp32_len()
    tilex_ii = 1
    for bn in range(vec_width*2, 0, -1):
        if N % bn == 0:
            tilex_ii = bn
            break
    NN = N // tilex_ii
    tilex_oi = 1
    while NN // tilex_oi > 4:
        if (NN // tilex_oi) % 2 == 1:
            break
        tilex_oi *= 2

    tiley_ii = 8
    while M % tiley_ii != 0:
        tiley_ii //= 2
    MM = M // tiley_ii
    tiley_oi = 1
    while MM // tiley_oi > 4:
        if (MM // tiley_oi) % 2 == 1:
            break
        tiley_oi *= 2

    cfg["tile_y"] = SplitEntity([MM // tiley_oi, tiley_oi, tiley_ii])
    cfg["tile_x"] = SplitEntity([NN // tilex_oi, tilex_oi, tilex_ii])
    cfg["tile_k"] = SplitEntity([K, 1])


def _default_dense_nopack_config(cfg, M, N, K):
    # Generate default schedule for dynamic shape.
    if isinstance(M, tvm.expr.Var):
        M = 16
    if isinstance(N, tvm.expr.Var):
        N = 16
    if isinstance(K, tvm.expr.Var):
        K = 16

    vec_width = get_fp32_len()
    tilek_bn = 1
    for bn in range(vec_width*2, 0, -1):
        if K % bn == 0:
            tilek_bn = bn
            break
    cfg["tile_k"] = SplitEntity([K // tilek_bn, tilek_bn])
    cfg["tile_x"] = SplitEntity([N, 1])
    cfg["tile_y"] = SplitEntity([1, M])

@autotvm.register_topi_compute2("dense_nopack.x86")
def dense_nopack(cfg, data, weight, bias=None, out_dtype=None):
    if out_dtype is None:
        out_dtype = data.dtype
    M, K = get_const_tuple(data.shape)
    N, _ = get_const_tuple(weight.shape)
    # create tuning space
    cfg.define_split("tile_y", M, num_outputs=2)
    cfg.define_split("tile_x", N, num_outputs=2)
    cfg.define_split("tile_k", K, num_outputs=2)
    if cfg.is_fallback:
        _default_dense_nopack_config(cfg, M, N, K)

    vec = cfg["tile_k"].size[-1]
    k = tvm.reduce_axis((0, K // vec), "k")
    CC = tvm.compute((M, N, vec),
                     lambda z, y, x: tvm.sum(
                         data[z, k * vec + x].astype(out_dtype) *
                         weight[y, k * vec + x].astype(out_dtype), axis=k))

    kk = tvm.reduce_axis((0, vec), "kk")
    C = tvm.compute((M, N),
                    lambda y, x: tvm.sum(CC[y, x, kk], axis=kk),
                    tag="dense_nopack")
    if bias is not None:
        C = tvm.compute((M, N), lambda i, j: C[i, j] + bias[j].astype(out_dtype),
                        tag=tag.BROADCAST)
    return C


@autotvm.register_topi_schedule2("dense_nopack.x86")
def schedule_dense_nopack(cfg, outs):
    s = tvm.create_schedule([x.op for x in outs])

    def _callback(op):
        if 'dense_nopack' in op.tag:
            _schedule_dense_nopack_template(cfg, s, op.output(0))
    traverse_inline(s, outs[0].op, _callback)
    return s

@autotvm.register_topi_compute2("dense_pack.x86")
def dense_pack(cfg, data, weight, bias=None, out_dtype=None):
    if out_dtype is None:
        out_dtype = data.dtype
    M, K = get_const_tuple(data.shape) # batch, in_dim
    N, _ = get_const_tuple(weight.shape) # out_dim
    # create tuning space
    cfg.define_split("tile_y", M, num_outputs=3)
    cfg.define_split("tile_x", N, num_outputs=3)
    cfg.define_split("tile_k", K, num_outputs=2)
    if cfg.is_fallback:
        _default_dense_pack_config(cfg, M, N, K)

    packw_bn = cfg["tile_x"].size[-1]
    packw_shape = (N // packw_bn, K, packw_bn)
    packw = tvm.compute(packw_shape,
                        lambda z, y, x: weight[z * packw_bn + x, y], name="packed_weight")

    idxdiv = tvm.indexdiv
    idxmod = tvm.indexmod
    k = tvm.reduce_axis((0, K), name="k")
    C = tvm.compute((M, N),
                    lambda y, x: tvm.sum(
                        data[y, k].astype(out_dtype) *
                        packw[idxdiv(x, packw_bn), k, idxmod(x, packw_bn)].astype(out_dtype),
                        axis=k),
                    tag="dense_pack")
    if bias is not None:
        C = tvm.compute((M, N), lambda i, j: C[i, j] + bias[j].astype(out_dtype),
                        tag=tag.BROADCAST)
    return C

@autotvm.register_topi_schedule2("dense_pack.x86")
def schedule_dense_pack(cfg, outs):
    s = tvm.create_schedule([x.op for x in outs])

    def _callback(op):
        if "dense_pack" in op.tag:
            _schedule_dense_pack_template(cfg, s, op.output(0))
    traverse_inline(s, outs[0].op, _callback)
    return s

@autotvm.register_topi_compute2("dense_cblas.x86")
def dense_cblas(cfg, data, weight, bias=None, out_dtype=None):
    M, K = get_const_tuple(data.shape)
    N, _ = get_const_tuple(weight.shape)
    cfg.add_flop(M * K * N * 2)
    C = cblas.matmul(data, weight, False, True)
    if bias is not None:
        C = tvm.compute(C.shape, lambda i, j: C[i, j] + bias[j].astype(out_dtype),
                        tag=tag.BROADCAST)
    return C

@autotvm.register_topi_schedule2("dense_cblas.x86")
def schedule_dense_cblas(_, outs):
    return generic.schedule_extern(outs)
