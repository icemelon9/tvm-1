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
"""Definition of generic operator strategy."""
# pylint: disable=invalid-name,unused-argument
from __future__ import absolute_import

import topi
from topi.util import get_const_int, get_const_float, get_const_tuple, get_float_tuple
from .. import op as _op
from ....target import generic_func, override_native_generic_func

def wrap_topi_schedule(topi_schedule):
    """Wrap TOPI schedule which doesn't use attrs"""
    def wrapper(attrs, outs, target):
        with target:
            return topi_schedule(outs)
    return wrapper

def get_conv2d_out_depth(kernel, kernel_layout):
    weight_shape = get_const_tuple(kernel.shape)
    # NHWC layout
    if kernel_layout.startswith("HW"):
        return weight_shape[2] * weight_shape[3]
    # NCHW layout.
    # in ARM CPU contrib_spatial_pack schedule, we will prepack weight layout
    if len(weight_shape) == 4:
        return weight_shape[0] * weight_shape[1]
    else:
        assert len(weight_shape) == 5
        C, M, _, _, VC = weight_shape
        return C * VC * M

@generic_func
def schedule_injective(attrs, outs, target):
    """Schedule injective ops"""
    with target:
        return topi.generic.schedule_injective(outs)

@generic_func
def schedule_reduce(attrs, outs, target):
    """Schedule reduction ops"""
    with target:
        return topi.generic.schedule_reduce(outs)

_op._schedule_injective = schedule_injective
_op._schedule_reduce = schedule_reduce

# concatenate
@generic_func
def schedule_concatenate(attrs, outs, target):
    """Schedule concatenate op"""
    with target:
        return topi.generic.schedule_injective(outs)

# pool
@generic_func
def schedule_pool(attrs, outs, target):
    """Schedule pooling ops"""
    with target:
        return topi.generic.schedule_pool(outs, attrs.layout)

# pool_grad
@generic_func
def schedule_pool_grad(attrs, outs, target):
    """Schedule pooling gradient ops"""
    with target:
        return topi.generic.schedule_pool_grad(outs)

# adaptive pool
@generic_func
def schedule_adaptive_pool(attrs, outs, target):
    """Schedule adaptive pooling ops"""
    with target:
        return topi.generic.schedule_adaptive_pool(outs)

# softmax
@generic_func
def schedule_softmax(attrs, outs, target):
    """Schedule softmax"""
    with target:
        return topi.generic.schedule_softmax(outs)

# lrn
@generic_func
def schedule_lrn(attrs, outs, target):
    """Schedule LRN op"""
    with target:
        return topi.generic.schedule_lrn(outs)

# l2_normalize
@generic_func
def schedule_l2_normalize(attrs, outs, target):
    """Schedule L2 normalize op"""
    with target:
        return topi.generic.schedule_l2_normalize(outs)

# bitpack
@generic_func
def schedule_bitpack(attrs, outs, target):
    """Schedule bitpack"""
    with target:
        return topi.generic.schedule_bitpack(outs)

# conv2d
def wrap_compute_conv2d(topi_compute, has_group=False):
    """Wrap conv2d topi compute"""
    def _compute_conv2d(attrs, inputs, out_type):
        padding = get_const_tuple(attrs.padding)
        strides = get_const_tuple(attrs.strides)
        dilation = get_const_tuple(attrs.dilation)
        layout = attrs.data_layout
        out_dtype = attrs.out_dtype
        out_dtype = (inputs[0].dtype if out_dtype in ("same", "")
                     else out_dtype)
        if has_group:
            out = topi_compute(inputs[0], inputs[1], strides, padding, dilation, out_dtype)
        else:
            out = topi_compute(inputs[0], inputs[1], strides, padding, dilation,
                               layout, out_dtype)
        return [out]
    return _compute_conv2d

@override_native_generic_func("conv2d_strategy")
def conv2d_strategy(attrs, inputs, out_type, target):
    """conv2d generic strategy"""
    strategy = _op.OpStrategy()

    dilation = get_const_tuple(attrs.dilation)
    groups = attrs.groups
    layout = attrs.data_layout
    kernel_layout = attrs.kernel_layout

    assert layout in ["NCHW", "NHWC", "NCHW4c", "HWCN"]
    (dilation_h, dilation_w) = dilation
    if dilation_h < 1 or dilation_w < 1:
        raise ValueError("dilation should be positive value")

    if groups == 1:
        if layout == "NCHW" or layout == "NCHW4c":
            strategy.add_implement(
                wrap_compute_conv2d(topi.nn.conv2d),
                wrap_topi_schedule(topi.generic.schedule_conv2d_nchw))
        elif layout == "NHWC":
            strategy.add_implement(
                wrap_compute_conv2d(topi.nn.conv2d),
                wrap_topi_schedule(topi.generic.schedule_conv2d_nhwc))
        elif layout == "HWCN":
            strategy.add_implement(
                wrap_compute_conv2d(topi.nn.conv2d),
                wrap_topi_schedule(topi.generic.schedule_conv2d_hwcn))
    else:
        if layout == "NCHW" and get_conv2d_out_depth(inputs[1], kernel_layout) == groups:
            strategy.add_implement(
                wrap_compute_conv2d(topi.nn.depthwise_conv2d_nchw, True),
                wrap_topi_schedule(topi.generic.schedule_depthwise_conv2d_nchw))
        elif layout == "NHWC" and kernel_layout == "HWOI" \
                and get_conv2d_out_depth(inputs[1], kernel_layout) == groups:
            strategy.add_implement(
                wrap_compute_conv2d(topi.nn.depthwise_conv2d_nhwc, True),
                wrap_topi_schedule(topi.generic.schedule_depthwise_conv2d_nhwc))
        elif layout in ['NCHW', 'NCHW4c']:
            strategy.add_implement(
                wrap_compute_conv2d(topi.nn.group_conv2d_nchw, True),
                wrap_topi_schedule(topi.generic.schedule_group_conv2d_nchw))
        else:
            raise RuntimeError("Unsupported group number %d" % groups)

    return strategy

# conv2d_NCHWc
def wrap_compute_conv2d_NCHWc(topi_compute):
    """wrap conv2d_NCHWc topi compute"""
    def _compute_conv2d_NCHWc(attrs, inputs, out_type):
        padding = attrs.get_int_tuple("padding")
        strides = attrs.get_int_tuple("strides")
        dilation = attrs.get_int_tuple("dilation")
        data_layout = attrs.get_str("data_layout")
        out_layout = attrs.get_str("out_layout")
        out_dtype = attrs.get_str("out_dtype")
        out_dtype = inputs[0].dtype if out_dtype == "" else out_dtype
        return [topi_compute(inputs[0], inputs[1], strides, padding, dilation,
                             data_layout, out_layout, out_dtype)]
    return _compute_conv2d_NCHWc

@override_native_generic_func("conv2d_NCHWc_strategy")
def conv2d_NCHWc_strategy(attrs, inputs, out_type, target):
    """conv2d_NCHWc generic strategy"""
    strategy = _op.OpStrategy()
    if out_type == "int8":
        strategy.add_implement(
            wrap_compute_conv2d_NCHWc(topi.nn.conv2d_NCHWc_int8_compute),
            wrap_topi_schedule(topi.generic.schedule_conv2d_NCHWc_int8))
    else:
        strategy.add_implement(
            wrap_compute_conv2d_NCHWc(topi.nn.conv2d_NCHWc_compute),
            wrap_topi_schedule(topi.generic.schedule_conv2d_NCHWc))
    return strategy

# depthwise_conv2d_NCHWc
def wrap_compute_depthwise_conv2d_NCHWc(topi_func):
    """wrap depthwise_conv2d_NCHWc topi compute"""
    def compute(attrs, inputs, out_type):
        padding = attrs.get_int_tuple("padding")
        strides = attrs.get_int_tuple("strides")
        dilation = attrs.get_int_tuple("dilation")
        data_layout = attrs.get_str("data_layout")
        out_layout = attrs.get_str("out_layout")
        out_dtype = attrs.get_str("out_dtype")
        out_dtype = inputs[0].dtype if out_dtype == "" else out_dtype
        return [topi_func(inputs[0], inputs[1], strides, padding, dilation,
                          data_layout, out_layout, out_dtype)]
    return compute

@override_native_generic_func("depthwise_conv2d_NCHWc_strategy")
def depthwise_conv2d_NCHWc_strategy(attrs, inputs, out_type, target):
    """depthwise_conv2d generic strategy"""
    strategy = _op.OpStrategy()
    if out_type == "int8":
        pass
    else:
        strategy.add_implement(
            wrap_compute_depthwise_conv2d_NCHWc(topi.nn.depthwise_conv2d_NCHWc),
            wrap_topi_schedule(topi.generic.schedule_depthwise_conv2d_NCHWc))
    return strategy

# deformable_conv2d
def wrap_compute_deformable_conv2d(topi_compute):
    """wrap deformable_conv2d topi compute"""
    def _compute_deformable_conv2d(attrs, inputs, out_dtype):
        assert attrs.data_layout == "NCHW"
        padding = get_const_tuple(attrs.padding)
        strides = get_const_tuple(attrs.strides)
        dilation = get_const_tuple(attrs.dilation)
        deformable_groups = attrs.deformable_groups
        groups = attrs.groups
        out_dtype = attrs.out_dtype
        out_dtype = inputs[0].dtype if out_dtype in ("same", "") else out_dtype
        out = topi_compute(inputs[0], inputs[1], inputs[2], strides, padding,
                           dilation, deformable_groups, groups, out_dtype)
        return [out]
    return _compute_deformable_conv2d

@override_native_generic_func("deformable_conv2d_strategy")
def deformable_conv2d_strategy(attrs, inputs, out_type, target):
    """deformable_conv2d generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_deformable_conv2d(topi.nn.deformable_conv2d_nchw),
                           wrap_topi_schedule(topi.generic.schedule_deformable_conv2d_nchw))
    return strategy

# conv3d
def wrap_compute_conv3d(topi_compute):
    """wrap conv3d topi compute"""
    def _compute_conv3d(attrs, inputs, out_type):
        padding = get_const_tuple(attrs.padding)
        strides = get_const_tuple(attrs.strides)
        dilation = get_const_tuple(attrs.dilation)
        groups = attrs.groups
        layout = attrs.data_layout
        out_dtype = attrs.out_dtype
        out_dtype = (inputs[0].dtype if out_dtype in ("same", "")
                     else out_dtype)

        (dilation_d, dilation_h, dilation_w) = dilation
        if dilation_d < 1 or dilation_h < 1 or dilation_w < 1:
            raise ValueError("Dilation should be positive value")

        if groups == 1:
            out = topi_compute(inputs[0], inputs[1], strides, padding, dilation,
                               layout, out_dtype)
        else:
            raise ValueError("Not support arbitrary group number for now")
        return [out]
    return _compute_conv3d

@override_native_generic_func("conv3d_strategy")
def conv3d_strategy(attrs, inputs, out_type, target):
    """conv3d generic strategy"""
    strategy = _op.OpStrategy()
    layout = attrs.data_layout
    if layout == "NCDHW":
        strategy.add_implement(wrap_compute_conv3d(topi.nn.conv3d_ncdhw),
                               wrap_topi_schedule(topi.generic.schedule_conv3d_ncdhw))
    elif layout == "NDHWC":
        strategy.add_implement(wrap_compute_conv3d(topi.nn.conv3d_ndhwc),
                               wrap_topi_schedule(topi.generic.schedule_conv3d_ndhwc))
    else:
        raise ValueError("Not support this layout {} yet".format(layout))
    return strategy

# conv1d_transpose
def wrap_compute_conv1d_transpose(topi_compute):
    """wrap conv1d_transpose topi compute"""
    def _compute_conv1d_tranpsoe(attrs, inputs, out_type):
        padding = get_const_tuple(attrs.padding)
        strides = get_const_tuple(attrs.strides)
        out_dtype = attrs.out_dtype
        out_dtype = (inputs[0].dtype if out_dtype in ("same", "") else out_dtype)
        out = topi_compute(inputs[0], inputs[1], strides, padding, out_dtype)
        output_padding = get_const_tuple(attrs.output_padding)
        out = topi.nn.pad(out, [0, 0, 0], [0, 0, output_padding[0]])
        return [out]
    return _compute_conv1d_tranpsoe

@override_native_generic_func("conv1d_transpose_strategy")
def conv1d_transpose_strategy(attrs, inputs, out_type, target):
    """conv1d_transpose generic strategy"""
    strategy = _op.OpStrategy()
    layout = attrs.data_layout
    dilation = get_const_tuple(attrs.dilation)
    groups = attrs.groups
    assert layout == "NCW", "conv1d_transpose ncw only supported"
    assert dilation == (1,), "conv1d_transpose dilation is not supported"
    assert groups == 1, "conv1d_transpose groups == 1 only supported"
    strategy.add_implement(wrap_compute_conv1d_transpose(topi.nn.conv1d_transpose_ncw),
                           wrap_topi_schedule(topi.generic.schedule_conv1d_transpose_ncw))
    return strategy

# dense
def wrap_compute_dense(topi_compute):
    """wrap dense topi compute"""
    def _compute_dense(attrs, inputs, out_type):
        """Compute definition of dense"""
        out_dtype = attrs.out_dtype
        out_dtype = inputs[0].dtype if out_dtype == "" else out_dtype
        return [topi_compute(inputs[0], inputs[1], None, out_dtype)]
    return _compute_dense

@override_native_generic_func("dense_strategy")
def dense_strategy(attrs, inputs, out_type, target):
    """dense generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_dense(topi.nn.dense),
                           wrap_topi_schedule(topi.generic.schedule_dense))
    return strategy

# batch_matmul
def wrap_compute_batch_matmul(topi_func):
    """wrap batch_matmul topi compute"""
    def _compute_batch_matmul(attrs, inputs, out_type):
        return [topi_func(inputs[0], inputs[1])]
    return _compute_batch_matmul

@override_native_generic_func("batch_matmul_strategy")
def batch_matmul_strategy(attrs, inputs, out_type, target):
    """batch_matmul generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_batch_matmul(topi.nn.batch_matmul),
                           wrap_topi_schedule(topi.generic.schedule_batch_matmul))
    return strategy

# sparse_dense
@generic_func
def schedule_sparse_dense(attrs, outs, target):
    """schedule sparse_dense"""
    with target:
        return topi.generic.schedule_sparse_dense(outs)

# sparse_transpose
@generic_func
def schedule_sparse_transpose(attrs, outs, target):
    """schedule sparse_transpose"""
    with target:
        return topi.generic.schedule_sparse_transpose(outs)

# argsort
def wrap_compute_argsort(topi_compute):
    """Wrap argsort topi compute"""
    def _compute_argsort(attrs, inputs, _):
        axis = get_const_int(attrs.axis)
        is_ascend = bool(get_const_int(attrs.is_ascend))
        dtype = attrs.dtype
        return [topi_compute(inputs[0], axis=axis, is_ascend=is_ascend, dtype=dtype)]
    return _compute_argsort

@override_native_generic_func("argsort_strategy")
def argsort_strategy(attrs, inputs, out_type, target):
    """argsort generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_argsort(topi.argsort),
                           wrap_topi_schedule(topi.generic.schedule_argsort))
    return strategy

# topk
def wrap_compute_topk(topi_func):
    """Wrap topk compute"""
    def _compute_topk(attrs, inputs, out_type):
        k = get_const_int(attrs.k)
        axis = get_const_int(attrs.axis)
        ret_type = attrs.ret_type
        is_ascend = bool(get_const_int(attrs.is_ascend))
        dtype = attrs.dtype
        out = topi_func(inputs[0], k, axis, ret_type, is_ascend, dtype)
        out = out if isinstance(out, list) else [out]
        return out
    return _compute_topk

@override_native_generic_func("topk_strategy")
def topk_strategy(attrs, inputs, out_type, target):
    """topk generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_topk(topi.topk),
                           wrap_topi_schedule(topi.generic.schedule_topk))
    return strategy

# multibox_prior
@generic_func
def schedule_multibox_prior(attrs, outs, target):
    """schedule multibox_prior"""
    with target:
        return topi.generic.schedule_multibox_prior(outs)

# multibox_transform_loc
@generic_func
def schedule_multibox_transform_loc(attrs, outs, target):
    """schedule multibox_transform_loc"""
    with target:
        return topi.generic.schedule_multibox_transform_loc(outs)

# get_valid_counts
def wrap_compute_get_valid_counts(topi_compute):
    """wrap get_valid_counts topi compute"""
    def _compute_get_valid_counts(attrs, inputs, out_type):
        score_threshold = get_const_float(attrs.score_threshold)
        id_index = get_const_int(attrs.id_index)
        score_index = get_const_int(attrs.score_index)
        return topi_compute(inputs[0], score_threshold, id_index, score_index)
    return _compute_get_valid_counts

@override_native_generic_func("get_valid_counts_strategy")
def get_valid_counts_strategy(attrs, inputs, out_type, target):
    """get_valid_counts generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_get_valid_counts(topi.vision.get_valid_counts),
                           wrap_topi_schedule(topi.generic.schedule_get_valid_counts))
    return strategy

# non-maximum suppression
def wrap_compute_nms(topi_compute):
    """wrap nms topi compute"""
    def _compute_nms(attrs, inputs, out_type):
        return_indices = bool(get_const_int(attrs.return_indices))
        max_output_size = get_const_int(attrs.max_output_size)
        iou_threshold = get_const_float(attrs.iou_threshold)
        force_suppress = bool(get_const_int(attrs.force_suppress))
        top_k = get_const_int(attrs.top_k)
        coord_start = get_const_int(attrs.coord_start)
        score_index = get_const_int(attrs.score_index)
        id_index = get_const_int(attrs.id_index)
        invalid_to_bottom = bool(get_const_int(attrs.invalid_to_bottom))
        return [topi_compute(inputs[0], inputs[1], max_output_size, iou_threshold,
                             force_suppress, top_k, coord_start, score_index,
                             id_index, return_indices, invalid_to_bottom)]
    return _compute_nms

@override_native_generic_func("non_max_suppression_strategy")
def nms_strategy(attrs, inputs, out_type, target):
    """nms generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_nms(topi.vision.non_max_suppression),
                           wrap_topi_schedule(topi.generic.schedule_nms))
    return strategy

# roi_align
def wrap_compute_roi_align(topi_compute):
    """wrap roi_align topi compute"""
    def _compute_roi_align(attrs, inputs, out_type):
        assert attrs.layout == "NCHW"
        pooled_size = get_const_tuple(attrs.pooled_size)
        return [topi_compute(inputs[0], inputs[1],
                             pooled_size=pooled_size,
                             spatial_scale=attrs.spatial_scale,
                             sample_ratio=attrs.sample_ratio)]
    return _compute_roi_align

@override_native_generic_func("roi_align_strategy")
def roi_align_strategy(attrs, inputs, out_type, target):
    """roi_align generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_roi_align(topi.vision.rcnn.roi_align_nchw),
                           wrap_topi_schedule(topi.generic.schedule_roi_align))
    return strategy

# roi_pool
@generic_func
def schedule_roi_pool(attrs, outs, target):
    """schedule roi_pool"""
    with target:
        return topi.generic.schedule_roi_pool(outs)

# proposal
def wrap_compute_proposal(topi_compute):
    """wrap proposal topi compute"""
    def _compute_proposal(attrs, inputs, out_type):
        scales = get_float_tuple(attrs.scales)
        ratios = get_float_tuple(attrs.ratios)
        feature_stride = attrs.feature_stride
        threshold = attrs.threshold
        rpn_pre_nms_top_n = attrs.rpn_pre_nms_top_n
        rpn_post_nms_top_n = attrs.rpn_post_nms_top_n
        rpn_min_size = attrs.rpn_min_size
        iou_loss = bool(get_const_int(attrs.iou_loss))
        return [topi_compute(inputs[0], inputs[1], inputs[2], scales, ratios,
                             feature_stride, threshold, rpn_pre_nms_top_n,
                             rpn_post_nms_top_n, rpn_min_size, iou_loss)]
    return _compute_proposal

@override_native_generic_func("proposal_strategy")
def proposal_strategy(attrs, inputs, out_type, target):
    """proposal generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_proposal(topi.vision.rcnn.proposal),
                           wrap_topi_schedule(topi.generic.schedule_proposal))
    return strategy

# argwhere
@generic_func
def schedule_argwhere(attrs, outs, target):
    """schedule argwhere"""
    with target:
        return topi.generic.schedule_argwhere(outs)

# bitserial_dense
def wrap_compute_bitserial_dense(topi_compute):
    """wrap bitserial_dense topi compute"""
    def compute_bitserial_dense(attrs, inputs, out_type):
        """Compute definition of bitserial dense"""
        data_bits = attrs.data_bits
        weight_bits = attrs.weight_bits
        pack_dtype = attrs.pack_dtype
        out_dtype = attrs.out_dtype
        out_dtype = inputs[0].dtype if out_dtype == "" else out_dtype
        unipolar = attrs.unipolar
        return [topi_compute(inputs[0], inputs[1], data_bits, weight_bits,
                             pack_dtype, out_dtype, unipolar)]
    return compute_bitserial_dense

@override_native_generic_func("bitserial_dense_strategy")
def bitserial_dense_strategy(attrs, inputs, out_type, target):
    """bitserial_dense generic strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(
        wrap_compute_bitserial_dense(topi.nn.bitserial_dense),
        wrap_topi_schedule(topi.generic.schedule_bitserial_dense))
    return strategy
