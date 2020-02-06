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
"""Definition of bifrost operator strategy."""
# pylint: disable=invalid-name,unused-argument,wildcard-import,unused-wildcard-import
import topi
from .generic import *
from .. import op as _op


@conv2d_strategy.register("bifrost")
def conv2d_strategy_bifrost(attrs, inputs, out_type, target):
    """conv2d mali(bifrost) strategy"""
    strategy = _op.OpStrategy()
    data, kernel = inputs
    dilation_h, dilation_w = attrs.get_int_tuple("dilation")
    stride_h, stride_w = attrs.get_int_tuple("strides")
    groups = attrs.groups
    layout = attrs.data_layout
    kernel_layout = attrs.kernel_layout
    if dilation_h < 1 or dilation_w < 1:
        raise ValueError("dilation should be positive value")

    if groups == 1:
        if layout == "NCHW":
            assert kernel_layout == "OIHW"
            strategy.add_implement(
                wrap_compute_conv2d(topi.bifrost.conv2d_nchw_spatial_pack),
                wrap_topi_schedule(topi.bifrost.schedule_conv2d_nchw_spatial_pack),
                name="conv2d_nchw_spatial_pack.bifrost")

            _, _, kh, kw = get_const_tuple(kernel.shape)
            if kh == 3 and kw == 3 and stride_h == 1 and stride_w == 1 and \
                    dilation_h == 1 and dilation_w == 1:
                strategy.add_implement(
                    wrap_compute_conv2d(topi.bifrost.conv2d_nchw_winograd),
                    wrap_topi_schedule(topi.bifrost.schedule_conv2d_nchw_winograd),
                    name="conv2d_nchw_winograd.bifrost",
                    plevel=15)
        else:
            raise RuntimeError("Unsupported conv2d layout {} for Mali(Bifrost)".
                               format(layout))
    elif is_depthwise_conv2d(data.shape, layout, kernel.shape, kernel_layout, groups):
        if layout == "NCHW":
            assert kernel_layout == "OIHW"
            strategy.add_implement(
                wrap_compute_conv2d(topi.nn.depthwise_conv2d_nchw),
                wrap_topi_schedule(topi.bifrost.schedule_depthwise_conv2d_nchw),
                name="depthwise_conv2d_nchw.bifrost")
        else:
            raise RuntimeError("Unsupported depthwise_conv2d layout {} for Mali(Bifrost)".
                               format(layout))
    else: # group_conv2d
        raise RuntimeError("group_conv2d is not supported for Mali(Bifrost)")
    return strategy

@conv2d_winograd_without_weight_transfrom_strategy.register("bifrost")
def conv2d_winograd_without_weight_transfrom_strategy_bifrost(attrs, inputs, out_type, target):
    """conv2d_winograd_without_weight_transfrom mali(bifrost) strategy"""
    dilation = attrs.get_int_tuple("dilation")
    groups = attrs.get_int("groups")
    layout = attrs.data_layout
    stride_h, stride_w = attrs.get_int_tuple("strides")
    assert dilation == (1, 1), "Do not support dilate now"
    assert groups == 1, "Do not supoort arbitrary group number"
    strategy = _op.OpStrategy()
    if layout == "NCHW":
        _, _, kh, kw = get_const_tuple(inputs[1].shape)
        assert kh == 3 and kw == 3 and stride_h == 1 and stride_w == 1
        strategy.add_implement(
            wrap_compute_conv2d(topi.bifrost.conv2d_nchw_winograd),
            wrap_topi_schedule(topi.bifrost.schedule_conv2d_nchw_winograd),
            name="conv2d_nchw_winograd.bifrost")
    else:
        raise RuntimeError("Unsupported conv2d_winograd_without_weight_transfrom layout {}".
                           format(layout))
    return strategy

@dense_strategy.register("bifrost")
def dense_strategy_bifrost(attrs, inputs, out_type, target):
    """dense mali(bifrost) strategy"""
    strategy = _op.OpStrategy()
    strategy.add_implement(wrap_compute_dense(topi.bifrost.dense),
                           wrap_topi_schedule(topi.bifrost.schedule_dense),
                           name="dense.bifrost")
    return strategy
