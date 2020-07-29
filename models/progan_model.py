import torch
from torch.autograd import grad

from .base_model import BaseModel
from . import networks
import numpy as np

from .progan_layers import update_average

try:
    from apex import amp
except ImportError:
    print("Please install NVIDIA Apex for safe mixed precision if you want to use non default --opt_level")


class ProGanModel(BaseModel):
    """
    This is an implementation of the paper "Progressive Growing of GANs": https://arxiv.org/abs/1710.10196.
    Model requires dataset of type dataset_mode='single', generator netG='progan', discriminator netD='progan'.
    Please note that opt.crop_size (default 256) == 4 * 2 ** opt.max_steps (default max_steps is 6).
    ngf and ndf controlls dimensions of the backbone (128-512).
    Network G is a master-generator (accumulates weights for eval) and network C (stands for current) is a
    current trainable generator.

    See also:
        https://github.com/tkarras/progressive_growing_of_gans
        https://github.com/odegeasslbc/Progressive-GAN-pytorch
    """

    @staticmethod
    def modify_commandline_options(parser, is_train=True):

        parser.set_defaults(netG='progan', netD='progan', dataset_mode='single', beta1=0., ngf=512, ndf=512,
                            gan_mode='relhinge')
        parser.add_argument('--z_dim', type=int, default=128, help='random noise dim')
        parser.add_argument('--max_steps', type=int, default=6, help='steps of growing')
        parser.add_argument('--steps_schedule', type=str, default='linear',
                            help='type of when to turn to the next step: linear or fibonacci')
        return parser

    def __init__(self, opt):

        BaseModel.__init__(self, opt)
        self.z_dim = opt.z_dim
        self.max_steps = opt.max_steps
        # specify the training losses you want to print out. The training/test scripts will call <BaseModel.get_current_losses>
        self.loss_names = ['G_GAN', 'D_real', 'D_fake', 'D']
        self.loss_D_real, self.loss_D_fake, self.loss_G_GAN, self.loss_D, self.loss_G = 0, 0, 0, 0, 0
        if self.opt.gan_mode == 'wgangp':
            self.loss_names.append('D_gradpen')
        # specify the images you want to save/display. The training/test scripts will call <BaseModel.get_current_visuals>
        self.visual_names = ['fake_B', 'real_B']
        # specify the models you want to save to the disk. The training/test scripts will call <BaseModel.save_networks> and <BaseModel.load_networks>
        if self.isTrain:
            self.model_names = ['G', 'D', 'C']
        else:  # during test time, only load G
            self.model_names = ['G']

        if self.isTrain:
            assert opt.crop_size == 4 * 2 ** self.max_steps
            assert opt.beta1 == 0

        # define networks (both generator and discriminator)
        self.netG = networks.define_G(opt.z_dim, opt.input_nc, opt.ngf, opt.netG, opt.norm,
                                      not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids,
                                      init_weights=False, max_steps=self.max_steps)
        """
        resulting generator, not for training, just for eval
        """

        if self.isTrain:
            self.netC = networks.define_G(opt.z_dim, opt.input_nc, opt.ngf, opt.netG, opt.norm,
                                          not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids,
                                          init_weights=False, max_steps=self.max_steps)
            """
            current training generator
            """
            self.netD = networks.define_D(opt.input_nc, opt.ndf, opt.netD,
                                          opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, self.gpu_ids,
                                          init_weights=False, max_steps=self.max_steps)
            """
            current training discr.
            """

        if self.isTrain:
            # define loss functions
            self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)
            self.criterionL1 = torch.nn.L1Loss()
            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            self.optimizer_C = torch.optim.Adam(self.netC.parameters(), lr=opt.lr, betas=(opt.beta1, 0.99), eps=1e-6)
            self.optimizer_D = torch.optim.Adam(self.netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.99), eps=1e-6)
            self.optimizers.append(self.optimizer_C)
            self.optimizers.append(self.optimizer_D)

            if opt.apex:
                [self.netC, self.netD], [self.optimizer_C, self.optimizer_D] = amp.initialize(
                    [self.netC, self.netD], [self.optimizer_C, self.optimizer_D], opt_level=opt.opt_level, num_losses=2)

        self.make_data_parallel()

        # inner counters
        self.total_steps = opt.n_epochs + opt.n_epochs_decay + 1
        """
        total epochs
        """
        self.step = 0
        """
        current step of network, 1-6
        """
        self.iter = 0
        """
        current iter, 0-(total epochs)//6
        """
        self.alpha = 0.
        """
        current alpha rate to fuse different scales
        """
        self.epochs_schedule = self.create_epochs_schedule(opt.steps_schedule)
        """
        schedule when to turn to the next step
        """

        if self.isTrain:
            assert self.total_steps > 12
            assert self.opt.crop_size % 2 ** self.max_steps == 0

        # set fusing
        self.netG.eval()
        self.accumulate(0)

    def set_input(self, input):
        """Unpack input data from the dataloader and perform necessary pre-processing steps.

        Parameters:
            input (dict): include the data itself and its metadata information.

        The option 'direction' can be used to swap images in domain A and domain B.
        """
        AtoB = self.opt.direction == 'AtoB'
        self.real_B = input['A' if AtoB else 'B'].to(self.device)
        self.real_B = self.__progressive_downsampling(self.real_B, self.step, self.alpha)
        #self.real_B = F.interpolate(self.real_B, size=(4 * 2 ** self.step, 4 * 2 ** self.step), mode='bilinear')
        self.image_paths = input['A_paths' if AtoB else 'B_paths']

    def forward(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        net = self.netC if self.isTrain else self.netG
        step = self.step if self.isTrain else self.max_steps
        alpha = self.alpha if self.isTrain else 1
        batch_size = self.real_B.size(0)
        z = torch.randn((batch_size, self.z_dim, self.opt.crop_size // (2 ** self.max_steps),
                         self.opt.crop_size // (2 ** self.max_steps)),
                        device=self.device)
        # z = torch.randn(batch_size, 512).to(self.device)
        self.fake_B = net(z, step=step, alpha=alpha)

    def backward_D(self):
        """Calculate GAN loss for the discriminator"""
        # Fake; stop backprop to the generator by detaching fake_B
        fake_B = self.fake_B
        pred_fake = self.netD(fake_B.detach(), step=self.step, alpha=self.alpha)
        # Real
        real_B = self.real_B
        self.pred_real = self.netD(real_B, step=self.step, alpha=self.alpha)

        if self.opt.gan_mode != 'relhinge':
            self.loss_D_fake = self.criterionGAN(pred_fake, False)
            self.loss_D_real = self.criterionGAN(self.pred_real, True)
            if self.opt.gan_mode == 'wgangp':
                # some correction of D loss
                self.loss_D_real += 0.001 * (self.pred_real ** 2).mean()
            # combine loss and calculate gradients
            self.loss_D = (self.loss_D_fake + self.loss_D_real) * 0.5
        else:
            self.loss_D = self.criterionGAN(self.pred_real, None, other_pred=pred_fake)

        if self.opt.gan_mode == 'wgangp':
            ### gradient penalty for D
            b_size = fake_B.size(0)
            eps = torch.rand(b_size, 1, 1, 1, dtype=fake_B.dtype, device=fake_B.device).to(fake_B.device)
            x_hat = eps * real_B.data + (1 - eps) * fake_B.detach().data
            x_hat.requires_grad = True
            hat_predict = self.netD(x_hat, step=self.step, alpha=self.alpha)
            grad_x_hat = grad(
                outputs=hat_predict.sum(), inputs=x_hat, create_graph=True)[0]
            grad_penalty = ((grad_x_hat.view(grad_x_hat.size(0), -1)
                             .norm(2, dim=1) - 1) ** 2).mean()
            self.loss_D_gradpen = 10 * grad_penalty
            self.loss_D += self.loss_D_gradpen

        if not (torch.isinf(self.loss_D) or torch.isnan(self.loss_D) or torch.mean(torch.abs(self.loss_D)) > 100):
            if self.opt.apex:
                with amp.scale_loss(self.loss_D, self.optimizer_D, loss_id=0) as loss_D_scaled:
                    loss_D_scaled.backward()
            else:
                self.loss_D.backward()

    def backward_G(self):
        """Calculate GAN loss for the generator"""
        # First, G(A) should fake the discriminator
        fake_B = self.fake_B
        pred_fake = self.netD(fake_B, step=self.step, alpha=self.alpha)
        if self.opt.gan_mode != 'relhinge':
            self.loss_G_GAN = self.criterionGAN(pred_fake, True)
        else:
            self.loss_G_GAN = self.criterionGAN(pred_fake, None, other_pred=self.pred_real.detach())
        self.loss_G = self.loss_G_GAN

        if not (torch.isinf(self.loss_G) or torch.isnan(self.loss_G) or torch.mean(torch.abs(self.loss_G)) > 100):
            if self.opt.apex:
                with amp.scale_loss(self.loss_G, self.optimizer_C, loss_id=1) as loss_G_scaled:
                    loss_G_scaled.backward()
            else:
                self.loss_G.backward()

    def optimize_parameters(self):
        self.forward()  # compute fake images: G(A)
        # update D
        self.set_requires_grad(self.netD, True)  # enable backprop for D
        self.optimizer_D.zero_grad()  # set D's gradients to zero
        self.backward_D()  # calculate gradients for D
        self.optimizer_D.step()  # update D's weights
        # update generator C
        self.set_requires_grad(self.netD, False)  # D requires no gradients when optimizing G
        self.optimizer_C.zero_grad()  # set G's gradients to zero
        self.backward_G()  # calculate graidents for G
        self.optimizer_C.step()  # udpate G's weights
        self.accumulate()  # fuse params

    def create_epochs_schedule(self, steps_schedule_type):
        if steps_schedule_type == 'fibonacci':
            basic_weights = np.array([3., 3., 3., 5., 8., 13., 21., 34., 55.])
        else:
            basic_weights = np.array([1, 1, 1, 1, 1, 1, 1, 1, 1])
        basic_weights = basic_weights[:self.max_steps + 1]
        epochs_schedule = self.total_steps * basic_weights / np.sum(basic_weights)
        epochs_schedule = epochs_schedule.astype(np.int)
        print('schedule of step turning: %s' % str(epochs_schedule))
        return epochs_schedule

    def update_inners_counters(self):
        """
        Update counters of iterations
        """
        self.iter += 1
        self.alpha = min(1, (2. / (self.epochs_schedule[self.step])) * self.iter)
        if self.iter > self.epochs_schedule[self.step]:
            print('turn to step %s' % str(self.step + 1))
            self.alpha = 0
            self.iter = 0
            self.step += 1

            if self.step > self.max_steps:
                self.alpha = 1
                self.step = self.max_steps

        print('new alpha: %s, new step: %s' % (self.alpha, self.step))

    def accumulate(self, decay=0.999):
        """
        Accumulate weights from self.C to self.G with decay
        @param decay decay
        """
        update_average(self.netG, self.netC, decay)

    def update_learning_rate(self):
        super(ProGanModel, self).update_learning_rate()
        self.update_inners_counters()

    def __progressive_downsampling(self, real_batch, depth, alpha):
        """
        private helper for downsampling the original images in order to facilitate the
        progressive growing of the layers.
        :param real_batch: batch of real samples
        :param depth: depth at which training is going on
        :param alpha: current value of the fader alpha
        :return: real_samples => modified real batch of samples
        """

        from torch.nn import AvgPool2d
        from torch.nn.functional import interpolate

        # downsample the real_batch for the given depth
        down_sample_factor = int(np.power(2, self.max_steps - depth))
        prior_downsample_factor = max(int(np.power(2, self.max_steps - depth + 1)), 0)

        ds_real_samples = AvgPool2d(down_sample_factor)(real_batch)

        if depth > 0:
            prior_ds_real_samples = interpolate(AvgPool2d(prior_downsample_factor)(real_batch),
                                                scale_factor=2)
        else:
            prior_ds_real_samples = ds_real_samples

        # real samples are a combination of ds_real_samples and prior_ds_real_samples
        real_samples = (alpha * ds_real_samples) + ((1 - alpha) * prior_ds_real_samples)

        # return the so computed real_samples
        return real_samples