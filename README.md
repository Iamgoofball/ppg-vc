# ppg-vc
Phonetic PosteriorGram (PPG)-Based Voice Conversion (VC)

This repo implements different kinds of PPG-based VC models. The PPG model provided in `conformer_ppg_model` is based on Hybrid CTC-Attention phoneme recognizer, trained with LibriSpeech (960hrs). This repo uses HifiGAN as the vocoder model.

## Highlights
- Any-to-many VC
- Any-to-Any VC (a.k.a. few/one-shot VC)

## How to use
### Data preprocessing
- Please run `1_compute_ctc_att_bnf.py` to compute PPG features.
- Please run `2_compute_f0.py` to compute fundamental frequency.
- Please run `3_compute_spk_dvecs.py` to compute speaker d-vectors.

### Training
- Please refer to `run.sh`

### Conversion
- Plesae refer to `test.sh`

## TODO
- [ ] Upload pretraind models.

## Citations
```
@ARTICLE{liu2021any,
  author={Liu, Songxiang and Cao, Yuewen and Wang, Disong and Wu, Xixin and Liu, Xunying and Meng, Helen},
  journal={IEEE/ACM Transactions on Audio, Speech, and Language Processing}, 
  title={Any-to-Many Voice Conversion With Location-Relative Sequence-to-Sequence Modeling}, 
  year={2021},
  volume={29},
  number={},
  pages={1717-1728},
  doi={10.1109/TASLP.2021.3076867}
}
