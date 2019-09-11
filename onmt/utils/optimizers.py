""" Optimizers class """
import torch
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
import operator
import functools
from copy import copy
from math import sqrt

from onmt.utils.misc import fn_args


def build_torch_optimizer(model, opt):
    """Builds the PyTorch optimizer.

    We use the default parameters for Adam that are suggested by
    the original paper https://arxiv.org/pdf/1412.6980.pdf
    These values are also used by other established implementations,
    e.g. https://www.tensorflow.org/api_docs/python/tf/train/AdamOptimizer
    https://keras.io/optimizers/
    Recently there are slightly different values used in the paper
    "Attention is all you need"
    https://arxiv.org/pdf/1706.03762.pdf, particularly the value beta2=0.98
    was used there however, beta2=0.999 is still arguably the more
    established value, so we use that here as well

    Args:
      model: The model to optimize.
      opt. The dictionary of options.

    Returns:
      A ``torch.optim.Optimizer`` instance.
    """
    if opt.disc_ft > 0 and not (opt.optim == 'adam' or opt.optim == 'fusedadam'):
        raise NotImplementedError

    if opt.disc_ft > 0 and opt.share_decoder_embeddings and (opt.simple_fusion):
        raise NotImplementedError

    if opt.disc_ft > 0 and opt.share_decoder_embeddings and opt.copy_attn and opt.full_gen_bias:
        raise NotImplementedError

    if opt.disc_ft > 0 and 'transformer' not in opt.decoder_type:
        raise NotImplementedError

    params = [p for p in model.parameters() if p.requires_grad]
    betas = [opt.adam_beta1, opt.adam_beta2]
    if opt.optim == 'sgd':
        optimizer = optim.SGD(params, lr=opt.learning_rate)
    elif opt.optim == 'adagrad':
        optimizer = optim.Adagrad(
            params,
            lr=opt.learning_rate,
            initial_accumulator_value=opt.adagrad_accumulator_init)
    elif opt.optim == 'adadelta':
        optimizer = optim.Adadelta(params, lr=opt.learning_rate)
    elif opt.optim == 'adafactor':
        optimizer = AdaFactor(
            params,
            non_constant_decay=True,
            enable_factorization=True,
            weight_decay=0)
    elif opt.optim == 'adam' or opt.optim == 'fusedadam':
        if opt.disc_ft > 0:
            if opt.encdec_share_params:
                enc_params = []
            else:
                if hasattr(model, 'encoder'):
                    if opt.share_embeddings:
                        enc_params = [p for name, p in model.encoder.named_parameters() if 'embeddings' not in name]
                    else:
                        enc_params = [p for p in model.encoder.parameters()]
                else:
                    enc_params = []

            decoder = model.decoder
            if enc_params:
                param_groups = [{'params': enc_params, 'factor': 1.0}]
            else:
                param_groups = []

            # Making a choice here to use smaller learning rate for generator weight if 
            # using shared decoder embeddings
            if opt.share_decoder_embeddings:
                if opt.full_gen_bias:
                    gen_params = [model.generator[0].bias]
                else:
                    if opt.copy_attn:
                        gen_params = [p for p in model.generator.linear_copy.parameters()]
                    else:
                        gen_params = []
            else:
                gen_params = model.generator.parameters()

            params_end = [*gen_params, *decoder.layer_norm.parameters()]

            if opt.full_context_lr:
                params_end += [p for name, p in decoder.named_parameters() if 'context' in name or 'ctx' in name]

            factor = 1.0/opt.dec_lr_factor
            param_groups.append({'params': params_end, 'factor': factor})
            for layer_num in range(opt.dec_layers-1, -1, -1):
                factor /= opt.disc_ft
                if opt.full_context_lr:
                     layer_params = [p for name, p in decoder.transformer_layers[layer_num].named_parameters() if 'context' not in name and 'ctx' not in name]
                else:
                     layer_params = [p for p in decoder.transformer_layers[layer_num].parameters()]

                param_groups.append({'params': layer_params, 'factor': factor})

            factor /= opt.disc_ft
            emb_params = [p for p in decoder.embeddings.parameters()]
            if opt.share_decoder_embeddings and not opt.full_gen_bias:
                if opt.copy_attn:
                    emb_params.append(model.generator.linear.bias)
                else:
                    emb_params.append(model.generator[0].bias)
            param_groups.append({'params': emb_params, 'factor': factor})

            num_params = 0
            for group in param_groups:
                for p in group['params']:
                    if not p.requires_grad:
                        continue
                    num_params += p.nelement()

            print('num params for optimizer: %d' % num_params)
        else:
            param_groups = params
        
        if opt.optim == 'adam':
            optimizer = optim.Adam(
                param_groups,
                lr=opt.learning_rate,
                betas=betas,
                eps=1e-9)
        elif opt.optim == 'fusedadam':
            import apex
            optimizer = apex.optimizers.FusedAdam(
                param_groups,
                lr=opt.learning_rate,
                betas=betas)

    elif opt.optim == 'sparseadam':
        dense = []
        sparse = []
        for name, param in model.named_parameters():
            if not param.requires_grad:
                continue
            # TODO: Find a better way to check for sparse gradients.
            if 'embed' in name:
                sparse.append(param)
            else:
                dense.append(param)
        optimizer = MultipleOptimizer(
            [optim.Adam(
                dense,
                lr=opt.learning_rate,
                betas=betas,
                eps=1e-8),
             optim.SparseAdam(
                 sparse,
                 lr=opt.learning_rate,
                 betas=betas,
                 eps=1e-8)])
    else:
        raise ValueError('Invalid optimizer type: ' + opt.optim)

    if opt.model_dtype == 'fp16':
        import apex
        static_loss_scale = opt.loss_scale
        dynamic_loss_scale = opt.loss_scale == 0
        # TODO: clean this up when APEX unify its optimizer API.
        if opt.optim.startswith('fused'):
            namespace = apex.optimizers  # Faster wrapper.
        else:
            namespace = apex.fp16_utils
        optimizer = namespace.FP16_Optimizer(
            optimizer,
            static_loss_scale=static_loss_scale,
            dynamic_loss_scale=dynamic_loss_scale)
    return optimizer


def make_learning_rate_decay_fn(opt):
    """Returns the learning decay function from options."""
    if opt.decay_method == 'noam':
        return functools.partial(
            noam_decay,
            warmup_steps=opt.warmup_steps,
            model_size=opt.rnn_size)
    elif opt.decay_method == 'rsqrt':
        return functools.partial(
            rsqrt_decay, warmup_steps=opt.warmup_steps)
    elif opt.decay_method == 'stlr':
        if opt.warmup_steps > opt.train_steps:
            raise ValueError('warmup_steps should be smaller than train_steps')
        return functools.partial(
            stlr_decay, warmup_steps=opt.warmup_steps,
            train_steps=opt.train_steps, ratio=opt.stlr_ratio)
    elif opt.decay_method == 'invsq':
        return functools.partial(
            invsq_decay,
            warmup_steps=opt.warmup_steps,
            warmup_init_factor=opt.warmup_init_factor)
    elif opt.start_decay_steps is not None:
        return functools.partial(
            exponential_decay,
            rate=opt.learning_rate_decay,
            decay_steps=opt.decay_steps,
            start_step=opt.start_decay_steps)

def invsq_decay(step, warmup_steps, warmup_init_factor):
    if step < warmup_steps:
        return 1.0/warmup_init_factor + (1 - 1.0/warmup_init_factor)/warmup_steps*step
    else:
        return (warmup_steps/step)**0.5

def stlr_decay(step, warmup_steps, train_steps, ratio):
    cut = warmup_steps
    cut_frac = warmup_steps/train_steps
    p = min(step/cut, 1 - (step-cut)/(cut*(1/cut_frac-1)))
    return (1 + p*(ratio-1))/ratio

def noam_decay(step, warmup_steps, model_size):
    """Learning rate schedule described in
    https://arxiv.org/pdf/1706.03762.pdf.
    """
    return (
        model_size ** (-0.5) *
        min(step ** (-0.5), step * warmup_steps**(-1.5)))


def exponential_decay(step, rate, decay_steps, start_step=0):
    """A standard exponential decay, scaling the learning rate by :obj:`rate`
    every :obj:`decay_steps` steps.
    """
    return rate ** (max(step - start_step + decay_steps, 0) // decay_steps)


def rsqrt_decay(step, warmup_steps):
    """Decay based on the reciprocal of the step square root."""
    return 1.0 / sqrt(max(step, warmup_steps))


class MultipleOptimizer(object):
    """ Implement multiple optimizers needed for sparse adam """

    def __init__(self, op):
        """ ? """
        self.optimizers = op

    @property
    def param_groups(self):
        param_groups = []
        for optimizer in self.optimizers:
            param_groups.extend(optimizer.param_groups)
        return param_groups

    def zero_grad(self):
        """ ? """
        for op in self.optimizers:
            op.zero_grad()

    def step(self):
        """ ? """
        for op in self.optimizers:
            op.step()

    @property
    def state(self):
        """ ? """
        return {k: v for op in self.optimizers for k, v in op.state.items()}

    def state_dict(self):
        """ ? """
        return [op.state_dict() for op in self.optimizers]

    def load_state_dict(self, state_dicts):
        """ ? """
        assert len(state_dicts) == len(self.optimizers)
        for i in range(len(state_dicts)):
            self.optimizers[i].load_state_dict(state_dicts[i])


class Optimizer(object):
    """
    Controller class for optimization. Mostly a thin
    wrapper for `optim`, but also useful for implementing
    rate scheduling beyond what is currently available.
    Also implements necessary methods for training RNNs such
    as grad manipulations.
    """

    def __init__(self,
                 optimizer,
                 learning_rate,
                 learning_rate_decay_fn=None,
                 max_grad_norm=None):
        """Initializes the controller.

       Args:
         optimizer: A ``torch.optim.Optimizer`` instance.
         learning_rate: The initial learning rate.
         learning_rate_decay_fn: An optional callable taking the current step
           as argument and return a learning rate scaling factor.
         max_grad_norm: Clip gradients to this global norm.
        """
        self._optimizer = optimizer
        self._learning_rate = learning_rate
        self._learning_rate_decay_fn = learning_rate_decay_fn
        self._max_grad_norm = max_grad_norm or 0
        self._training_step = 1
        self._decay_step = 1
        self._with_fp16_wrapper = (
            optimizer.__class__.__name__ == "FP16_Optimizer")

    @classmethod
    def from_opt(cls, model, opt, checkpoint=None):
        """Builds the optimizer from options.

        Args:
          cls: The ``Optimizer`` class to instantiate.
          model: The model to optimize.
          opt: The dict of user options.
          checkpoint: An optional checkpoint to load states from.

        Returns:
          An ``Optimizer`` instance.
        """
        optim_opt = opt
        optim_state_dict = None

        if opt.train_from and checkpoint is not None:
            optim = checkpoint['optim']
            ckpt_opt = checkpoint['opt']
            ckpt_state_dict = {}
            if isinstance(optim, Optimizer):  # Backward compatibility.
                ckpt_state_dict['training_step'] = optim._step + 1
                ckpt_state_dict['decay_step'] = optim._step + 1
                ckpt_state_dict['optimizer'] = optim.optimizer.state_dict()
            else:
                ckpt_state_dict = optim

            if opt.reset_optim == 'none':
                # Load everything from the checkpoint.
                optim_opt = ckpt_opt
                optim_state_dict = ckpt_state_dict
            elif opt.reset_optim == 'all':
                # Build everything from scratch.
                pass
            elif opt.reset_optim == 'states':
                # Reset optimizer, keep options.
                optim_opt = ckpt_opt
                optim_state_dict = ckpt_state_dict
                del optim_state_dict['optimizer']
            elif opt.reset_optim == 'keep_states':
                # Reset options, keep optimizer.
                optim_state_dict = ckpt_state_dict

        optimizer = cls(
            build_torch_optimizer(model, optim_opt),
            optim_opt.learning_rate,
            learning_rate_decay_fn=make_learning_rate_decay_fn(optim_opt),
            max_grad_norm=optim_opt.max_grad_norm)
        if optim_state_dict:
            optimizer.load_state_dict(optim_state_dict)
        return optimizer

    @property
    def training_step(self):
        """The current training step."""
        return self._training_step

    def learning_rate(self):
        """Returns the current learning rate."""
        if self._learning_rate_decay_fn is None:
            return self._learning_rate
        scale = self._learning_rate_decay_fn(self._decay_step)
        return scale * self._learning_rate

    def state_dict(self):
        return {
            'training_step': self._training_step,
            'decay_step': self._decay_step,
            'optimizer': self._optimizer.state_dict()
        }

    def load_state_dict(self, state_dict):
        self._training_step = state_dict['training_step']
        # State can be partially restored.
        if 'decay_step' in state_dict:
            self._decay_step = state_dict['decay_step']
        if 'optimizer' in state_dict:
            self._optimizer.load_state_dict(state_dict['optimizer'])

    def zero_grad(self):
        """Zero the gradients of optimized parameters."""
        self._optimizer.zero_grad()

    def backward(self, loss):
        """Wrapper for backward pass. Some optimizer requires ownership of the
        backward pass."""
        if self._with_fp16_wrapper:
            kwargs = {}
            if "update_master_grads" in fn_args(self._optimizer.backward):
                kwargs["update_master_grads"] = True
            self._optimizer.backward(loss, **kwargs)
        else:
            loss.backward()

    def step(self):
        """Update the model parameters based on current gradients.

        Optionally, will employ gradient modification or update learning
        rate.
        """
        learning_rate = self.learning_rate()
        if self._with_fp16_wrapper:
            if hasattr(self._optimizer, "update_master_grads"):
                self._optimizer.update_master_grads()
            if hasattr(self._optimizer, "clip_master_grads") and \
               self._max_grad_norm > 0:
                self._optimizer.clip_master_grads(self._max_grad_norm)
        for group in self._optimizer.param_groups:
            if 'factor' in group:
                group['lr'] = group['factor']*learning_rate
            else:
                group['lr'] = learning_rate
            if not self._with_fp16_wrapper and self._max_grad_norm > 0:
                clip_grad_norm_(group['params'], self._max_grad_norm)
        self._optimizer.step()
        self._decay_step += 1
        self._training_step += 1

# Code below is an implementation of https://arxiv.org/pdf/1804.04235.pdf
# inspired but modified from https://github.com/DeadAt0m/adafactor-pytorch


class AdaFactor(torch.optim.Optimizer):

    def __init__(self, params, lr=None, beta1=0.9, beta2=0.999, eps1=1e-30,
                 eps2=1e-3, cliping_threshold=1, non_constant_decay=True,
                 enable_factorization=True, ams_grad=True, weight_decay=0):

        enable_momentum = beta1 != 0

        if non_constant_decay:
            ams_grad = False

        defaults = dict(lr=lr, beta1=beta1, beta2=beta2, eps1=eps1,
                        eps2=eps2, cliping_threshold=cliping_threshold,
                        weight_decay=weight_decay, ams_grad=ams_grad,
                        enable_factorization=enable_factorization,
                        enable_momentum=enable_momentum,
                        non_constant_decay=non_constant_decay)

        super(AdaFactor, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(AdaFactor, self).__setstate__(state)

    def _experimental_reshape(self, shape):
        temp_shape = shape[2:]
        if len(temp_shape) == 1:
            new_shape = (shape[0], shape[1]*shape[2])
        else:
            tmp_div = len(temp_shape) // 2 + len(temp_shape) % 2
            new_shape = (shape[0]*functools.reduce(operator.mul,
                                                   temp_shape[tmp_div:], 1),
                         shape[1]*functools.reduce(operator.mul,
                                                   temp_shape[:tmp_div], 1))
        return new_shape, copy(shape)

    def _check_shape(self, shape):
        '''
        output1 - True - algorithm for matrix, False - vector;
        output2 - need reshape
        '''
        if len(shape) > 2:
            return True, True
        elif len(shape) == 2:
            return True, False
        elif len(shape) == 2 and (shape[0] == 1 or shape[1] == 1):
            return False, False
        else:
            return False, False

    def _rms(self, x):
        return sqrt(torch.mean(x.pow(2)))

    def step(self, closure=None):
        loss = None
        if closure is not None:
            loss = closure()
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data

                if grad.is_sparse:
                    raise RuntimeError('Adam does not support sparse \
                                       gradients, use SparseAdam instead')

                is_matrix, is_need_reshape = self._check_shape(grad.size())
                new_shape = p.data.size()
                if is_need_reshape and group['enable_factorization']:
                    new_shape, old_shape = \
                        self._experimental_reshape(p.data.size())
                    grad = grad.view(new_shape)

                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0
                    if group['enable_momentum']:
                        state['exp_avg'] = torch.zeros(new_shape,
                                                       dtype=torch.float32,
                                                       device=p.grad.device)

                    if is_matrix and group['enable_factorization']:
                        state['exp_avg_sq_R'] = \
                            torch.zeros((1, new_shape[1]),
                                        dtype=torch.float32,
                                        device=p.grad.device)
                        state['exp_avg_sq_C'] = \
                            torch.zeros((new_shape[0], 1),
                                        dtype=torch.float32,
                                        device=p.grad.device)
                    else:
                        state['exp_avg_sq'] = torch.zeros(new_shape,
                                                          dtype=torch.float32,
                                                          device=p.grad.device)
                    if group['ams_grad']:
                        state['exp_avg_sq_hat'] = \
                            torch.zeros(new_shape, dtype=torch.float32,
                                        device=p.grad.device)

                if group['enable_momentum']:
                    exp_avg = state['exp_avg']

                if is_matrix and group['enable_factorization']:
                    exp_avg_sq_r = state['exp_avg_sq_R']
                    exp_avg_sq_c = state['exp_avg_sq_C']
                else:
                    exp_avg_sq = state['exp_avg_sq']

                if group['ams_grad']:
                    exp_avg_sq_hat = state['exp_avg_sq_hat']

                state['step'] += 1
                lr_t = group['lr']
                lr_t *= max(group['eps2'], self._rms(p.data))

                if group['enable_momentum']:
                    if group['non_constant_decay']:
                        beta1_t = group['beta1'] * \
                                  (1 - group['beta1'] ** (state['step'] - 1)) \
                                  / (1 - group['beta1'] ** state['step'])
                    else:
                        beta1_t = group['beta1']
                    exp_avg.mul_(beta1_t).add_(1 - beta1_t, grad)

                if group['non_constant_decay']:
                    beta2_t = group['beta2'] * \
                              (1 - group['beta2'] ** (state['step'] - 1)) / \
                              (1 - group['beta2'] ** state['step'])
                else:
                    beta2_t = group['beta2']

                if is_matrix and group['enable_factorization']:
                    exp_avg_sq_r.mul_(beta2_t). \
                        add_(1 - beta2_t, torch.sum(torch.mul(grad, grad).
                                                    add_(group['eps1']),
                                                    dim=0, keepdim=True))
                    exp_avg_sq_c.mul_(beta2_t). \
                        add_(1 - beta2_t, torch.sum(torch.mul(grad, grad).
                                                    add_(group['eps1']),
                                                    dim=1, keepdim=True))
                    v = torch.mul(exp_avg_sq_c,
                                  exp_avg_sq_r).div_(torch.sum(exp_avg_sq_r))
                else:
                    exp_avg_sq.mul_(beta2_t). \
                        addcmul_(1 - beta2_t, grad, grad). \
                        add_((1 - beta2_t)*group['eps1'])
                    v = exp_avg_sq

                g = grad
                if group['enable_momentum']:
                    g = torch.div(exp_avg, 1 - beta1_t ** state['step'])

                if group['ams_grad']:
                    torch.max(exp_avg_sq_hat, v, out=exp_avg_sq_hat)
                    v = exp_avg_sq_hat
                    u = torch.div(g, (torch.div(v, 1 - beta2_t **
                                  state['step'])).sqrt().add_(group['eps1']))
                else:
                    u = torch.div(g, v.sqrt())

                u.div_(max(1, self._rms(u) / group['cliping_threshold']))
                p.data.add_(-lr_t * (u.view(old_shape) if is_need_reshape and
                            group['enable_factorization'] else u))

                if group['weight_decay'] != 0:
                    p.data.add_(-group['weight_decay'] * lr_t, p.data)

        return loss