import torch
from torch.autograd import Function
from torch.nn import Module
from numba import cuda
import os
import math
import pytest

os.environ["NUMBAPRO_LIBDEVICE"] = "/usr/local/cuda/nvvm/libdevice/"
os.environ["NUMBAPRO_NVVM"] = "/usr/local/cuda/nvvm/lib64/libnvvm.so.3.3.0"


@cuda.jit(
    "(float32[:,:,:,:], int32[:,:], float32[:,:,:], float32[:], int32[:], int32[:], int32, int32[:,:])"
)
def cu_kernel_forward(log_probs, labels, alpha, log_p, T, U, blank, lock):
    """
    Compute forward pass for the forward-backward algorithm using Numba cuda kernel.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    Arguments
    ---------
    log_probs : 4D Tensor of (batch x TimeLength x LabelLength x outputDim) from the Transducer network
    labels : 2D Tensor of (batch x MaxSeqLabelLength) containing targets of the batch with zero padding
    alpha : 3D Tensor of (batch x TimeLength x LabelLength) for forward computation
    log_p : 1D Tensor of (batch) for forward cost computation
    T: 1D Tensor of (batch) containing TimeLength of each target
    U: 1D Tensor of (batch) containing LabelLength of each target
    blank: blank indice
    lock: 2D Tensor of (batch x LabelLength) containing bool(1-0) lock for parallel computation
    """
    b = cuda.blockIdx.x
    u = cuda.threadIdx.x
    t = 0
    if u <= U[b]:
        while t < T[b]:
            if u == 0:
                if t == 0:
                    alpha[b, 0, 0] = 0
                else:
                    alpha[b, t, 0] = (
                        alpha[b, t - 1, 0] + log_probs[b, t - 1, 0, blank]
                    )
                cuda.atomic.add(lock, (b, u + 1), -1)
                t += 1
            else:
                if cuda.atomic.add(lock, (b, u), 0) < 0:
                    if t == 0:
                        alpha[b, 0, u] = (
                            alpha[b, 0, u - 1]
                            + log_probs[b, 0, u - 1, labels[b, u - 1]]
                        )
                    else:
                        emit = (
                            alpha[b, t, u - 1]
                            + log_probs[b, t, u - 1, labels[b, u - 1]]
                        )
                        no_emit = (
                            alpha[b, t - 1, u] + log_probs[b, t - 1, u, blank]
                        )
                        alpha[b, t, u] = max(no_emit, emit) + math.log1p(
                            math.exp(-abs(no_emit - emit))
                        )
                    if u < U[b]:
                        cuda.atomic.add(lock, (b, u + 1), -1)
                    cuda.atomic.add(lock, (b, u), 1)
                    t += 1
        if u == 0:
            log_p[b] = (
                alpha[b, T[b] - 1, U[b]] + log_probs[b, T[b] - 1, U[b], blank]
            )


@cuda.jit(
    "(float32[:,:,:,:], int32[:,:], float32[:,:,:], float32[:], int32[:], int32[:], int32, int32[:,:])"
)
def cu_kernel_backward(log_probs, labels, beta, log_p, T, U, blank, lock):
    """
    Compute backward pass for the forward-backward algorithm using Numba cuda kernel.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    Arguments
    ---------
    log_probs : 4D Tensor of (batch x TimeLength x LabelLength x outputDim) from the Transducer network
    labels : 2D Tensor of (batch x MaxSeqLabelLength) containing targets of the batch with zero padding
    beta : 3D Tensor of (batch x TimeLength x LabelLength) for backward computation
    log_p : 1D Tensor of (batch) for backward cost computation
    T: 1D Tensor of (batch) containing TimeLength of each target
    U: 1D Tensor of (batch) containing LabelLength of each target
    blank: blank indice
    lock: 2D Tensor of (batch x LabelLength) containing bool(1-0) lock for parallel computation
    """
    b = cuda.blockIdx.x
    u = cuda.threadIdx.x
    t = T[b] - 1
    if u <= U[b]:
        while t >= 0:
            if u == U[b]:
                if t == T[b] - 1:
                    beta[b, t, u] = log_probs[b, t, u, blank]
                else:
                    beta[b, t, u] = (
                        beta[b, t + 1, u] + log_probs[b, t, u, blank]
                    )
                cuda.atomic.add(lock, (b, u - 1), -1)
                t -= 1
            else:
                if cuda.atomic.add(lock, (b, u), 0) < 0:
                    if t == T[b] - 1:
                        beta[b, t, u] = (
                            beta[b, t, u + 1] + log_probs[b, t, u, labels[b, u]]
                        )
                    else:
                        emit = (
                            beta[b, t, u + 1] + log_probs[b, t, u, labels[b, u]]
                        )
                        no_emit = beta[b, t + 1, u] + log_probs[b, t, u, blank]
                        beta[b, t, u] = max(no_emit, emit) + math.log1p(
                            math.exp(-abs(no_emit - emit))
                        )
                    if u > 0:
                        cuda.atomic.add(lock, (b, u - 1), -1)
                    cuda.atomic.add(lock, (b, u), 1)
                    t -= 1
    if u == 0:
        log_p[b] = beta[b, 0, 0]


@cuda.jit(
    "(float32[:,:,:,:], int32[:,:],float32[:,:,:], float32[:,:,:], float32[:,:,:,:], int32[:], int32[:], int32)"
)
def cu_kernel_compute_grad(log_probs, labels, alpha, beta, grads, T, U, blank):
    """
    Compute gradient for the forward-backward algorithm using Numba cuda kernel.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    Arguments
    ---------
    log_probs : 4D Tensor of (batch x TimeLength x LabelLength x outputDim) from the Transducer network
    labels : 2D Tensor of (batch x MaxSeqLabelLength) containing targets of the batch with zero padding
    beta : 3D Tensor of (batch x TimeLength x LabelLength) for backward computation
    log_p : 1D Tensor of (batch) for backward cost computation
    T: 1D Tensor of (batch) containing TimeLength of each target
    U: 1D Tensor of (batch) containing LabelLength of each target
    blank: blank indice
    lock: 2D Tensor of (batch x LabelLength) containing bool(1-0) lock for parallel computation
    """

    b = cuda.blockIdx.x
    t = cuda.threadIdx.x
    if t < T[b]:
        if t == 0:
            grads[b, T[b] - 1, U[b], blank] = -math.exp(
                alpha[b, T[b] - 1, U[b]]
                + log_probs[b, T[b] - 1, U[b], blank]
                - beta[b, 0, 0]
            )

        # #if u < U[b] and t < T[b]-1:
        if t < T[b] - 1:
            for u in range(U[b] + 1):
                grads[b, t, u, blank] = alpha[b, t, u] + beta[b, t + 1, u]
                grads[b, t, u, blank] = -math.exp(
                    grads[b, t, u, blank]
                    + log_probs[b, t, u, blank]
                    - beta[b, 0, 0]
                )
        # # if k != blank
        # if t < T[b]:
        for u, l in enumerate(labels[b]):
            if u < U[b]:
                grads[b, t, u, l] = alpha[b, t, u] + beta[b, t, u + 1]
                grads[b, t, u, l] = -math.exp(
                    grads[b, t, u, l] + log_probs[b, t, u, l] - beta[b, 0, 0]
                )


class Transducer(Function):
    """
    This class implements the Transducer loss computation with forward-backward algorithm
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    This class use torch.autograd.Function. In fact of using the forward-backward algorithm,
    we need to compute the gradient manually.

    This class can't be instantiated, please refer to TransducerLoss class

    It is also possible to use this class directly by using Transducer.apply
    """

    @staticmethod
    def forward(ctx, log_probs, labels, T, U, blank, reduction):
        B, maxT, maxU, A = log_probs.shape
        grads = torch.zeros(
            (B, maxT, maxU, A), dtype=torch.float32, device=log_probs.device
        )
        alpha = torch.zeros((B, maxT, maxU), device=log_probs.device)
        beta = torch.zeros((B, maxT, maxU), device=log_probs.device)
        lock_alpha = torch.zeros(
            (B, maxU), dtype=torch.int32, device=log_probs.device
        )
        lock_beta = torch.zeros(
            (B, maxU), dtype=torch.int32, device=log_probs.device
        )
        log_p_alpha = torch.zeros((B,), device=log_probs.device)
        log_p_beta = torch.zeros((B,), device=log_probs.device)
        cu_kernel_forward[B, maxU](
            log_probs.detach(),
            labels,
            alpha,
            log_p_alpha,
            T,
            U,
            blank,
            lock_alpha,
        )
        cu_kernel_backward[B, maxU](
            log_probs.detach(), labels, beta, log_p_beta, T, U, blank, lock_beta
        )
        cu_kernel_compute_grad[B, maxT](
            log_probs.detach(), labels, alpha, beta, grads, T, U, blank
        )

        ctx.grads = grads
        if reduction == "mean":
            return (-(log_p_alpha + log_p_beta) / 2).mean()
        elif reduction == "sum":
            return sum(-(log_p_alpha + log_p_beta) / 2)
        elif reduction == "none":
            return -(log_p_alpha + log_p_beta) / 2
        else:
            raise Exception("Unexpected reduction {}".format(reduction))

    @staticmethod
    def backward(ctx, grad_output):
        grad_output = grad_output.view(-1, 1, 1, 1).to(ctx.grads)
        return ctx.grads.mul_(grad_output), None, None, None, None, None, None


def doctest_numba():
    try:
        cuda.cuda_paths
    except Exception:  # noqa: F401
        pytest.skip("TransducerLoss test fail, install numba")


class TransducerLoss(Module):
    """
    This class implements the Transduce loss computation with forward-backward algorithm.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    The TranducerLoss(nn.Module) use Transducer(autograd.Function)
    to compute the forward-backward loss and gradients.

    Exemple
    -------
    >>> import torch
    >>> from speechbrain.nnet.transducer.transducer_loss import TransducerLoss, doctest_numba
    >>> doctest_numba()
    >>> loss = TransducerLoss(blank=0)
    >>> acts = torch.randn((1,2,3,5)).cuda().log_softmax(dim=-1).requires_grad_()
    >>> labels = torch.Tensor([[1,2]]).cuda().int()
    >>> act_length = torch.Tensor([2]).cuda().int()
    >>> # U = label_length+1
    >>> label_length = torch.Tensor([2]).cuda().int()
    >>> l = loss(acts, labels, act_length, label_length)
    >>> l.backward()
    """

    def __init__(self, blank=0, reduction="mean"):
        super(TransducerLoss, self).__init__()
        self.blank = blank
        self.reduction = reduction
        self.loss = Transducer.apply
        try:
            cuda.cuda_paths
        except ImportError:
            err_msg = "cannot import numba. To use Transducer loss\n"
            err_msg += "=============================\n"
            err_msg += "If you use your localhost:\n"
            err_msg += "pip install numba\n"
            err_msg += (
                "export NUMBAPRO_LIBDEVICE='/usr/local/cuda/nvvm/libdevice/' \n"
            )
            err_msg += "export NUMBAPRO_NVVM='/usr/local/cuda/nvvm/lib64/libnvvm.so' \n"
            err_msg += "================================ \n"
            err_msg += "If you use conda:\n"
            err_msg += "conda install numba cudatoolkit=9.0"
            raise ImportError(err_msg)

    def forward(self, log_probs, labels, T, U):
        return self.loss(log_probs, labels, T, U, self.blank, self.reduction)
