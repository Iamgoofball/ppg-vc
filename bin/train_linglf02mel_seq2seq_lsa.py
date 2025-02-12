import os, sys
sys.path.append('/home/shaunxliu/projects/nnsp')
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
import torch
from torch.utils.data import DataLoader
import numpy as np
from src.solver import BaseSolver
from src.data_load import MultiSpkVcDataset, MultiSpkVcCollate
# from src.rnn_ppg2mel import BiRnnPpg2MelModel
# from nnsp.ctc_seq2seq_ppg_vc.mel_decoder_mol_encAddlf0 import MelDecoderMOL
from nnsp.ctc_seq2seq_ppg_vc.mel_decoder_lsa import MelDecoderLSA
from nnsp.ctc_seq2seq_ppg_vc.nnsp_model import MaskedMSELoss
from src.optim import Optimizer
from src.util import human_format, feat_to_fig


class Solver(BaseSolver):
    """Customized Solver."""
    def __init__(self, config, paras, mode):
        super().__init__(config, paras, mode)
        self.num_att_plots = 5
        self.att_ws_dir = f"{self.logdir}/att_ws"
        os.makedirs(self.att_ws_dir, exist_ok=True)
        self.best_loss = np.inf

    def fetch_data(self, data):
        """Move data to device"""
        data = [i.to(self.device) for i in data]
        return data

    def load_data(self):
        """ Load data for training/validation/plotting."""
        train_dataset = MultiSpkVcDataset(
            meta_file=self.config.data.train_fid_list,
            ppg_dir=self.config.data.ppg_dir,
            f0_dir=self.config.data.f0_dir,
            mel_dir=self.config.data.mel_dir,
            ppg_file_ext=self.config.data.ppg_file_ext,
        )
        dev_dataset = MultiSpkVcDataset(
            meta_file=self.config.data.dev_fid_list,
            ppg_dir=self.config.data.ppg_dir,
            f0_dir=self.config.data.f0_dir,
            mel_dir=self.config.data.mel_dir,
            ppg_file_ext=self.config.data.ppg_file_ext,
        )
        self.train_dataloader = DataLoader(
            train_dataset,
            num_workers=self.paras.njobs,
            shuffle=True,
            batch_size=self.config.hparas.batch_size,
            pin_memory=False,
            drop_last=True,
            collate_fn=MultiSpkVcCollate(self.config.model.frames_per_step),
        )
        self.dev_dataloader = DataLoader(
            dev_dataset,
            num_workers=self.paras.njobs,
            shuffle=False,
            batch_size=self.config.hparas.batch_size,
            pin_memory=False,
            drop_last=False,
            collate_fn=MultiSpkVcCollate(self.config.model.frames_per_step),
        )
        self.plot_dataloader = DataLoader(
            dev_dataset,
            num_workers=self.paras.njobs,
            shuffle=False,
            batch_size=1,
            pin_memory=False,
            drop_last=False,
            collate_fn=MultiSpkVcCollate(self.config.model.frames_per_step,
                                         give_uttids=True),
        )
        msg = "Have prepared training set and dev set."
        self.verbose(msg)
    
    def load_pretrained_params(self):
        prefix = "ppg2mel_model"
        ignore_layers = ["ppg2mel_model.spk_embedding.weight"]
        pretrain_model_file = self.config.data.pretrain_model_file
        pretrain_ckpt = torch.load(
            pretrain_model_file, map_location=self.device
        )
        model_dict = self.model.state_dict()
        
        # 1. filter out unnecessrary keys
        pretrain_dict = {k.split(".", maxsplit=1)[1]: v 
                         for k, v in pretrain_ckpt.items() if "spk_embedding" not in k 
                            and "wav2ppg_model" not in k and "reduce_proj" not in k}
        # print(len(pretrain_dict.keys()))
        # print(len((model_dict.keys())))
        # assert len(pretrain_dict.keys()) == len(model_dict.keys()) - 1

        # 2. overwrite entries in the existing state dict
        model_dict.update(pretrain_dict)

        # 3. load the new state dict
        self.model.load_state_dict(model_dict)

    def set_model(self):
        """Setup model and optimizer"""
        # Model
        self.model = MelDecoderLSA(
            **self.config["model"]
        ).to(self.device)
        # self.load_pretrained_params()

        # model_params = [{'params': self.model.spk_embedding.weight}]
        model_params = [{'params': self.model.parameters()}]
        
        # Loss criterion
        self.loss_criterion = MaskedMSELoss(self.config.model.frames_per_step)

        # Optimizer
        self.optimizer = Optimizer(model_params, **self.config["hparas"])
        self.verbose(self.optimizer.create_msg())

        # Automatically load pre-trained model if self.paras.load is given
        self.load_ckpt()

    def exec(self):
        self.verbose("Total training steps {}.".format(
            human_format(self.max_step)))

        mel_loss = None
        n_epochs = 0
        # Set as current time
        self.timer.set()
        
        while self.step < self.max_step:
            for data in self.train_dataloader:
                # Pre-step: updata lr_rate and do zero_grad
                lr_rate = self.optimizer.pre_step(self.step)
                total_loss = 0
                # data to device
                ppgs, lf0_uvs, mels, in_lengths, \
                    out_lengths, spk_ids, stop_tokens = self.fetch_data(data)
                self.timer.cnt("rd")
                mel_outputs, mel_outputs_postnet, predicted_stop = self.model(
                    ppgs,
                    in_lengths,
                    mels,
                    out_lengths,
                    lf0_uvs,
                    spk_ids
                ) 
                mel_loss, stop_loss = self.loss_criterion(
                    mel_outputs,
                    mel_outputs_postnet,
                    mels,
                    out_lengths,
                    stop_tokens,
                    predicted_stop
                )
                loss = mel_loss + stop_loss

                self.timer.cnt("fw")

                # Back-prop
                grad_norm = self.backward(loss)
                self.step += 1

                # Logger
                if (self.step == 1) or (self.step % self.PROGRESS_STEP == 0):
                    self.progress("Tr|loss:{:.4f},mel-loss:{:.4f},stop-loss:{:.4f}|Grad.Norm-{:.2f}|{}"
                                  .format(loss.cpu().item(), mel_loss.cpu().item(),
                                    stop_loss.cpu().item(), grad_norm, self.timer.show()))
                    self.write_log('loss', {'tr/loss': loss,
                                            'tr/mel-loss': mel_loss,
                                            'tr/stop-loss': stop_loss})

                # Validation
                if (self.step == 1) or (self.step % self.valid_step == 0):
                    self.validate()

                # End of step
                # https://github.com/pytorch/pytorch/issues/13246#issuecomment-529185354
                torch.cuda.empty_cache()
                self.timer.set()
                if self.step > self.max_step:
                    break
            n_epochs += 1
        self.log.close()

    def validate(self):
        self.model.eval()
        dev_loss, dev_mel_loss, dev_stop_loss = 0.0, 0.0, 0.0

        for i, data in enumerate(self.dev_dataloader):
            self.progress('Valid step - {}/{}'.format(i+1, len(self.dev_dataloader)))
            # Fetch data
            ppgs, lf0_uvs, mels, in_lengths, \
                out_lengths, spk_ids, stop_tokens = self.fetch_data(data)
            with torch.no_grad():
                mel_outputs, mel_outputs_postnet, predicted_stop = self.model(
                    ppgs,
                    in_lengths,
                    mels,
                    out_lengths,
                    lf0_uvs,
                    spk_ids
                ) 
                mel_loss, stop_loss = self.loss_criterion(
                    mel_outputs,
                    mel_outputs_postnet,
                    mels,
                    out_lengths,
                    stop_tokens,
                    predicted_stop
                )
                loss = mel_loss + stop_loss

                dev_loss += loss.cpu().item()
                dev_mel_loss += mel_loss.cpu().item()
                dev_stop_loss += stop_loss.cpu().item()

        dev_loss = dev_loss / (i + 1)
        dev_mel_loss = dev_mel_loss / (i + 1)
        dev_stop_loss = dev_stop_loss / (i + 1)
        self.save_checkpoint(f'step_{self.step}.pth', 'loss', dev_loss, show_msg=False)
        if dev_loss < self.best_loss:
            self.best_loss = dev_loss
            self.save_checkpoint(f'best_loss_step_{self.step}.pth', 'loss', dev_loss)
        self.write_log('loss', {'dv/loss': dev_loss,
                                'dv/mel-loss': dev_mel_loss,
                                'dv/stop-loss': dev_stop_loss})

        # plot attention
        for i, data in enumerate(self.plot_dataloader):
            if i == self.num_att_plots:
                break
            # Fetch data
            ppgs, lf0_uvs, mels, in_lengths, \
                out_lengths, spk_ids, stop_tokens = self.fetch_data(data[:-1])
            fid = data[-1][0]
            with torch.no_grad():
                _, _, _, att_ws = self.model(
                    ppgs,
                    in_lengths,
                    mels,
                    out_lengths,
                    lf0_uvs,
                    spk_ids,
                    output_att_ws=True
                )
                att_ws = att_ws.squeeze(0).cpu().numpy()
                att_ws = att_ws[None]
                w, h = plt.figaspect(1.0 / len(att_ws))
                fig = plt.Figure(figsize=(w * 1.3, h * 1.3))
                axes = fig.subplots(1, len(att_ws))
                if len(att_ws) == 1:
                    axes = [axes]

                for ax, aw in zip(axes, att_ws):
                    ax.imshow(aw.astype(np.float32), aspect="auto")
                    ax.set_title(f"{fid}")
                    ax.set_xlabel("Input")
                    ax.set_ylabel("Output")
                    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
                    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
                fig_name = f"{self.att_ws_dir}/{fid}_step{self.step}.png"
                fig.savefig(fig_name)
                
        # Resume training
        self.model.train()

