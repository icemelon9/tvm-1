"""Test code for batch_dot operator"""
import numpy as np
import tvm
import topi
import topi.testing
from topi.util import get_const_tuple
from tvm.contrib.pickle_memoize import memoize

from common import get_all_backend

def verify_batch_dot(batch, M, N, K):
    x = tvm.placeholder((batch, M, K), name='x')
    y = tvm.placeholder((batch, N, K), name='y')
    dtype = x.dtype

    # use memoize to pickle the test data for next time use
    @memoize("topi.tests.test_topi_batch_dot")
    def get_ref_data():
        a_np = np.random.uniform(size=(batch, M, K)).astype(dtype)
        b_np = np.random.uniform(size=(batch, N, K)).astype(dtype)
        c_np = np.zeros((batch, M, N)).astype(dtype)
        for i in range(batch):
            c_np[i] = np.dot(a_np[i], b_np[i].T)
        return (a_np, b_np, c_np)
    # get the test data
    a_np, b_np, c_np = get_ref_data()

    def check_device(device):
        ctx = tvm.context(device, 0)
        if not ctx.exist:
            print("Skip because %s is not enabled" % device)
            return
        print("Running on target: %s" % device)
        with tvm.target.create(device):
            out = topi.nn.batch_dot(x, y)
            s = topi.generic.schedule_batch_dot([out])
        a = tvm.nd.array(a_np, ctx)
        b = tvm.nd.array(b_np, ctx)
        c = tvm.nd.array(np.zeros(get_const_tuple(out.shape), dtype=dtype), ctx)
        f = tvm.build(s, [x, y, out], device, name="dense")
        f(a, b, c)
        tvm.testing.assert_allclose(c.asnumpy(), c_np, rtol=1e-5)

    for device in get_all_backend():
        check_device(device)

def test_batch_dot():
    verify_batch_dot(1, 16, 16, 32)
    verify_batch_dot(5, 16, 16, 32)
    verify_batch_dot(5, 16, 20, 32)


if __name__ == "__main__":
    test_batch_dot()
