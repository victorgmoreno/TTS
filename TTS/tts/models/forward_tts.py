from dataclasses import dataclass, field
from typing import Dict, Tuple

import torch
from coqpit import Coqpit
from torch import nn
from torch.cuda.amp.autocast_mode import autocast

from TTS.tts.layers.feed_forward.decoder import Decoder
from TTS.tts.layers.feed_forward.encoder import Encoder
from TTS.tts.layers.generic.aligner import AlignmentNetwork
from TTS.tts.layers.generic.pos_encoding import PositionalEncoding
from TTS.tts.layers.glow_tts.duration_predictor import DurationPredictor
from TTS.tts.models.base_tts import BaseTTS
from TTS.tts.utils.helpers import average_over_durations, generate_path, maximum_path, sequence_mask
from TTS.tts.utils.speakers import SpeakerManager
from TTS.tts.utils.visual import plot_alignment, plot_pitch, plot_spectrogram

from transformers import CamembertTokenizer, CamembertForTokenClassification, TokenClassificationPipeline
from TTS.tts.utils.text import sequence_to_text

@dataclass
class ForwardTTSArgs(Coqpit):
    """ForwardTTS Model arguments.

    Args:

        num_chars (int):
            Number of characters in the vocabulary. Defaults to 100.

        out_channels (int):
            Number of output channels. Defaults to 80.

        hidden_channels (int):
            Number of base hidden channels of the model. Defaults to 512.

        use_aligner (bool):
            Whether to use aligner network to learn the text to speech alignment or use pre-computed durations.
            If set False, durations should be computed by `TTS/bin/compute_attention_masks.py` and path to the
            pre-computed durations must be provided to `config.datasets[0].meta_file_attn_mask`. Defaults to True.

        use_pitch (bool):
            Use pitch predictor to learn the pitch. Defaults to True.

        duration_predictor_hidden_channels (int):
            Number of hidden channels in the duration predictor. Defaults to 256.

        duration_predictor_dropout_p (float):
            Dropout rate for the duration predictor. Defaults to 0.1.

        duration_predictor_kernel_size (int):
            Kernel size of conv layers in the duration predictor. Defaults to 3.

        pitch_predictor_hidden_channels (int):
            Number of hidden channels in the pitch predictor. Defaults to 256.

        pitch_predictor_dropout_p (float):
            Dropout rate for the pitch predictor. Defaults to 0.1.

        pitch_predictor_kernel_size (int):
            Kernel size of conv layers in the pitch predictor. Defaults to 3.

        pitch_embedding_kernel_size (int):
            Kernel size of the projection layer in the pitch predictor. Defaults to 3.

        positional_encoding (bool):
            Whether to use positional encoding. Defaults to True.

        positional_encoding_use_scale (bool):
            Whether to use a learnable scale coeff in the positional encoding. Defaults to True.

        length_scale (int):
            Length scale that multiplies the predicted durations. Larger values result slower speech. Defaults to 1.0.

        encoder_type (str):
            Type of the encoder module. One of the encoders available in :class:`TTS.tts.layers.feed_forward.encoder`.
            Defaults to `fftransformer` as in the paper.

        encoder_params (dict):
            Parameters of the encoder module. Defaults to ```{"hidden_channels_ffn": 1024, "num_heads": 1, "num_layers": 6, "dropout_p": 0.1}```

        decoder_type (str):
            Type of the decoder module. One of the decoders available in :class:`TTS.tts.layers.feed_forward.decoder`.
            Defaults to `fftransformer` as in the paper.

        decoder_params (str):
            Parameters of the decoder module. Defaults to ```{"hidden_channels_ffn": 1024, "num_heads": 1, "num_layers": 6, "dropout_p": 0.1}```

        detach_duration_predictor (bool):
            Detach the input to the duration predictor from the earlier computation graph so that the duraiton loss
            does not pass to the earlier layers. Defaults to True.

        max_duration (int):
            Maximum duration accepted by the model. Defaults to 75.

        num_speakers (int):
            Number of speakers for the speaker embedding layer. Defaults to 0.

        speakers_file (str):
            Path to the speaker mapping file for the Speaker Manager. Defaults to None.

        speaker_embedding_channels (int):
            Number of speaker embedding channels. Defaults to 256.

        use_d_vector_file (bool):
            Enable/Disable the use of d-vectors for multi-speaker training. Defaults to False.

        d_vector_dim (int):
            Number of d-vector channels. Defaults to 0.

    """

    num_chars: int = None
    out_channels: int = 80
    hidden_channels: int = 384
    use_aligner: bool = True

    # pitch params
    use_pitch: bool = True    
    pitch_predictor_hidden_channels: int = 256
    pitch_predictor_kernel_size: int = 3
    pitch_predictor_dropout_p: float = 0.1
    pitch_embedding_kernel_size: int = 3

    # energy params
    use_energy: bool = True
    energy_predictor_hidden_channels: int = 256
    energy_predictor_kernel_size: int = 3
    energy_predictor_dropout_p: float = 0.1
    energy_embedding_kernel_size: int = 3

    # duration params
    duration_predictor_hidden_channels: int = 256
    duration_predictor_kernel_size: int = 3
    duration_predictor_dropout_p: float = 0.1

    positional_encoding: bool = True
    poisitonal_encoding_use_scale: bool = True
    length_scale: int = 1
    encoder_type: str = "fftransformer"
    encoder_params: dict = field(
        default_factory=lambda: {"hidden_channels_ffn": 1024, "num_heads": 1, "num_layers": 6, "dropout_p": 0.1}
    )
    decoder_type: str = "fftransformer"
    decoder_params: dict = field(
        default_factory=lambda: {"hidden_channels_ffn": 1024, "num_heads": 1, "num_layers": 6, "dropout_p": 0.1}
    )
    detach_duration_predictor: bool = False
    max_duration: int = 75
    num_speakers: int = 1
    use_speaker_embedding: bool = False
    speakers_file: str = None
    use_d_vector_file: bool = False
    d_vector_dim: int = None
    d_vector_file: str = None
    use_pos_tagger: bool = False

class ForwardTTS(BaseTTS):
    """General forward TTS model implementation that uses an encoder-decoder architecture with an optional alignment
    network and a pitch predictor.

    If the alignment network is used, the model learns the text-to-speech alignment
    from the data instead of using pre-computed durations.

    If the pitch predictor is used, the model trains a pitch predictor that predicts average pitch value for each
    input character as in the FastPitch model.

    `ForwardTTS` can be configured to one of these architectures,

        - FastPitch
        - SpeedySpeech
        - FastSpeech
        - TODO: FastSpeech2 (requires average speech energy predictor)

    Args:
        config (Coqpit): Model coqpit class.
        speaker_manager (SpeakerManager): Speaker manager for multi-speaker training. Only used for multi-speaker models.
            Defaults to None.

    Examples:
        >>> from TTS.tts.models.fast_pitch import ForwardTTS, ForwardTTSArgs
        >>> config = ForwardTTSArgs()
        >>> model = ForwardTTS(config)
    """

    # pylint: disable=dangerous-default-value
    def __init__(self, config: Coqpit, speaker_manager: SpeakerManager = None):

        super().__init__(config)

        self.speaker_manager = speaker_manager
        self.init_multispeaker(config)

        self.max_duration = self.args.max_duration
        self.use_aligner = self.args.use_aligner
        self.use_pitch = self.args.use_pitch
        self.use_energy = self.args.use_energy
        self.use_binary_alignment_loss = False

        self.length_scale = (
            float(self.args.length_scale) if isinstance(self.args.length_scale, int) else self.args.length_scale
        )

        self.emb = nn.Embedding(self.args.num_chars, self.args.hidden_channels)

        self.encoder = Encoder(
            self.args.hidden_channels,
            self.args.hidden_channels,
            self.args.encoder_type,
            self.args.encoder_params,
            self.embedded_speaker_dim,
        )

        if self.args.positional_encoding:
            self.pos_encoder = PositionalEncoding(self.args.hidden_channels)

        self.decoder = Decoder(
            self.args.out_channels,
            self.args.hidden_channels,
            self.args.decoder_type,
            self.args.decoder_params,
        )

        self.duration_predictor = DurationPredictor(
            self.args.hidden_channels + self.embedded_speaker_dim,
            self.args.duration_predictor_hidden_channels,
            self.args.duration_predictor_kernel_size,
            self.args.duration_predictor_dropout_p,
        )

        if self.args.use_pitch:
            self.pitch_predictor = DurationPredictor(
                self.args.hidden_channels + self.embedded_speaker_dim,
                self.args.pitch_predictor_hidden_channels,
                self.args.pitch_predictor_kernel_size,
                self.args.pitch_predictor_dropout_p,
            )
            self.pitch_emb = nn.Conv1d(
                1,
                self.args.hidden_channels,
                kernel_size=self.args.pitch_embedding_kernel_size,
                padding=int((self.args.pitch_embedding_kernel_size - 1) / 2),
            )

        if self.args.use_energy:
            self.energy_predictor = DurationPredictor(
                self.args.hidden_channels + self.embedded_speaker_dim,
                self.args.energy_predictor_hidden_channels,
                self.args.energy_predictor_kernel_size,
                self.args.energy_predictor_dropout_p,
            )
            self.energy_emb = nn.Conv1d(
                1,
                self.args.hidden_channels,
                kernel_size=self.args.energy_embedding_kernel_size,
                padding=int((self.args.energy_embedding_kernel_size - 1) / 2),
            )

        if self.args.use_aligner:
            self.aligner = AlignmentNetwork(
                in_query_channels=self.args.out_channels, in_key_channels=self.args.hidden_channels
            )

        if self.args.use_pos_tagger:
            print("> Using POS Tagger")
            self.characters = config.characters
            self.add_blank = config.add_blank
            self.tokenizer = CamembertTokenizer.from_pretrained('qanastek/pos-french-camembert')
            self.pos_tagger = CamembertForTokenClassification.from_pretrained('qanastek/pos-french-camembert')

            if True: # Here goes freeze.pos in the future
                
                to_freeze = [self.pos_tagger, self.decoder, self.aligner]
                
                for block in to_freeze:
                    for param in block.parameters():
                        param.requires_grad = False
                
                print("POS, Decoder, Aligner and Duration Predictor are frozen")

            self.pos = TokenClassificationPipeline(model=self.pos_tagger, tokenizer=self.tokenizer, device = 'cpu')

            self.pos_embs = nn.Embedding(len(self.pos.model.config.id2label), self.args.hidden_channels)
            nn.init.zeros_(self.pos_embs.weight)

    def init_multispeaker(self, config: Coqpit):
        """Init for multi-speaker training.

        Args:
            config (Coqpit): Model configuration.
        """
        self.embedded_speaker_dim = 0
        # init speaker manager
        if self.speaker_manager is None and (config.use_d_vector_file or config.use_speaker_embedding):
            raise ValueError(
                " > SpeakerManager is not provided. You must provide the SpeakerManager before initializing a multi-speaker model."
            )
        # set number of speakers
        if self.speaker_manager is not None:
            self.num_speakers = self.speaker_manager.num_speakers
        # init d-vector embedding
        if config.use_d_vector_file:
            self.embedded_speaker_dim = config.d_vector_dim
            if self.args.d_vector_dim != self.args.hidden_channels:
                self.proj_g = nn.Conv1d(self.args.d_vector_dim, self.args.hidden_channels, 1)
        # init speaker embedding layer
        if config.use_speaker_embedding and not config.use_d_vector_file:
            print(" > Init speaker_embedding layer.")
            self.emb_g = nn.Embedding(self.args.num_speakers, self.args.hidden_channels)
            nn.init.uniform_(self.emb_g.weight, -0.1, 0.1)

    @staticmethod
    def generate_attn(dr, x_mask, y_mask=None):
        """Generate an attention mask from the durations.

        Shapes
           - dr: :math:`(B, T_{en})`
           - x_mask: :math:`(B, T_{en})`
           - y_mask: :math:`(B, T_{de})`
        """
        # compute decode mask from the durations
        if y_mask is None:
            y_lengths = dr.sum(1).long()
            y_lengths[y_lengths < 1] = 1
            y_mask = torch.unsqueeze(sequence_mask(y_lengths, None), 1).to(dr.dtype)
        attn_mask = torch.unsqueeze(x_mask, -1) * torch.unsqueeze(y_mask, 2)
        attn = generate_path(dr, attn_mask.squeeze(1)).to(dr.dtype)
        return attn

    def expand_encoder_outputs(self, en, dr, x_mask, y_mask):
        """Generate attention alignment map from durations and
        expand encoder outputs

        Shapes:
            - en: :math:`(B, D_{en}, T_{en})`
            - dr: :math:`(B, T_{en})`
            - x_mask: :math:`(B, T_{en})`
            - y_mask: :math:`(B, T_{de})`

        Examples::

            encoder output: [a,b,c,d]
            durations: [1, 3, 2, 1]

            expanded: [a, b, b, b, c, c, d]
            attention map: [[0, 0, 0, 0, 0, 0, 1],
                            [0, 0, 0, 0, 1, 1, 0],
                            [0, 1, 1, 1, 0, 0, 0],
                            [1, 0, 0, 0, 0, 0, 0]]
        """
        attn = self.generate_attn(dr, x_mask, y_mask)
        o_en_ex = torch.matmul(attn.squeeze(1).transpose(1, 2).to(en.dtype), en.transpose(1, 2)).transpose(1, 2)
        return o_en_ex, attn

    def format_durations(self, o_dr_log, x_mask):
        """Format predicted durations.
        1. Convert to linear scale from log scale
        2. Apply the length scale for speed adjustment
        3. Apply masking.
        4. Cast 0 durations to 1.
        5. Round the duration values.

        Args:
            o_dr_log: Log scale durations.
            x_mask: Input text mask.

        Shapes:
            - o_dr_log: :math:`(B, T_{de})`
            - x_mask: :math:`(B, T_{en})`
        """
        o_dr = (torch.exp(o_dr_log) - 1) * x_mask * self.length_scale
        o_dr[o_dr < 1] = 1.0
        o_dr = torch.round(o_dr)
        return o_dr

    def _forward_encoder(
        self, x: torch.LongTensor, x_mask: torch.FloatTensor, g: torch.FloatTensor = None
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Encoding forward pass.

        1. Embed speaker IDs if multi-speaker mode.
        2. Embed character sequences.
        3. Run the encoder network.
        4. Sum encoder outputs and speaker embeddings

        Args:
            x (torch.LongTensor): Input sequence IDs.
            x_mask (torch.FloatTensor): Input squence mask.
            g (torch.FloatTensor, optional): Conditioning vectors. In general speaker embeddings. Defaults to None.

        Returns:
            Tuple[torch.tensor, torch.tensor, torch.tensor, torch.tensor, torch.tensor]:
                encoder output, encoder output for the duration predictor, input sequence mask, speaker embeddings,
                character embeddings

        Shapes:
            - x: :math:`(B, T_{en})`
            - x_mask: :math:`(B, 1, T_{en})`
            - g: :math:`(B, C)`
        """
        if hasattr(self, "emb_g"):
            g = self.emb_g(g)  # [B, C, 1]
        if g is not None:
            g = g.unsqueeze(-1)
        # [B, T, C]
        x_emb = self.emb(x)
        # encoder pass
        o_en = self.encoder(torch.transpose(x_emb, 1, -1), x_mask)
        # speaker conditioning
        # TODO: try different ways of conditioning
        if g is not None:
            o_en = o_en + g
        return o_en, x_mask, g, x_emb

    def _forward_decoder(
        self,
        o_en: torch.FloatTensor,
        dr: torch.IntTensor,
        x_mask: torch.FloatTensor,
        y_lengths: torch.IntTensor,
        g: torch.FloatTensor,
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor]:
        """Decoding forward pass.

        1. Compute the decoder output mask
        2. Expand encoder output with the durations.
        3. Apply position encoding.
        4. Add speaker embeddings if multi-speaker mode.
        5. Run the decoder.

        Args:
            o_en (torch.FloatTensor): Encoder output.
            dr (torch.IntTensor): Ground truth durations or alignment network durations.
            x_mask (torch.IntTensor): Input sequence mask.
            y_lengths (torch.IntTensor): Output sequence lengths.
            g (torch.FloatTensor): Conditioning vectors. In general speaker embeddings.

        Returns:
            Tuple[torch.FloatTensor, torch.FloatTensor]: Decoder output, attention map from durations.
        """
        y_mask = torch.unsqueeze(sequence_mask(y_lengths, None), 1).to(o_en.dtype)
        # expand o_en with durations
        o_en_ex, attn = self.expand_encoder_outputs(o_en, dr, x_mask, y_mask)
        # positional encoding
        if hasattr(self, "pos_encoder"):
            o_en_ex = self.pos_encoder(o_en_ex, y_mask)
        # decoder pass
        o_de = self.decoder(o_en_ex, y_mask, g=g)
        return o_de.transpose(1, 2), attn.transpose(1, 2)

    def _forward_pitch_predictor(
        self,
        o_en: torch.FloatTensor,
        x_mask: torch.IntTensor,
        pitch: torch.FloatTensor = None,
        dr: torch.IntTensor = None,
        pitch_control: torch.FloatTensor = None,
        pitch_replace: torch.FloatTensor = None,
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor]:
        """Pitch predictor forward pass.

        1. Predict pitch from encoder outputs.
        2. In training - Compute average pitch values for each input character from the ground truth pitch values.
        3. Embed average pitch values.

        Args:
            o_en (torch.FloatTensor): Encoder output.
            x_mask (torch.IntTensor): Input sequence mask.
            pitch (torch.FloatTensor, optional): Ground truth pitch values. Defaults to None.
            dr (torch.IntTensor, optional): Ground truth durations. Defaults to None.

        Returns:
            Tuple[torch.FloatTensor, torch.FloatTensor]: Pitch embedding, pitch prediction.

        Shapes:
            - o_en: :math:`(B, C, T_{en})`
            - x_mask: :math:`(B, 1, T_{en})`
            - pitch: :math:`(B, 1, T_{de})`
            - dr: :math:`(B, T_{en})`
        """
        o_pitch = self.pitch_predictor(o_en, x_mask)
        if pitch is not None:
            avg_pitch = average_over_durations(pitch, dr)
            # Put the control over phonemes      
            if(pitch_control is not None):
                print('entrou no pitch control: ', pitch_control, avg_pitch)
                avg_pitch = avg_pitch*pitch_control 
            o_pitch_emb = self.pitch_emb(avg_pitch)
            return o_pitch_emb, o_pitch, avg_pitch
        if(pitch_control is not None):
            print('Pitch changed by args!')
            o_pitch = o_pitch + pitch_control # I don't remember why is it residual instead of replace, stay as TODO
        if(pitch_replace is not None):
            print("Pitch replaced by args!")
            o_pitch = pitch_replace
        o_pitch_emb = self.pitch_emb(o_pitch)
        return o_pitch_emb, o_pitch

    def _forward_energy_predictor(
        self,
        o_en: torch.FloatTensor,
        x_mask: torch.IntTensor,
        energy: torch.FloatTensor = None,
        dr: torch.IntTensor = None,
        energy_control: torch.FloatTensor = None,
        energy_replace: torch.FloatTensor = None,
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor]:
        """Energy predictor forward pass.

        1. Predict energy from encoder outputs.
        2. In training - Compute average pitch values for each input character from the ground truth pitch values.
        3. Embed average energy values.

        Args:
            o_en (torch.FloatTensor): Encoder output.
            x_mask (torch.IntTensor): Input sequence mask.
            energy (torch.FloatTensor, optional): Ground truth energy values. Defaults to None.
            dr (torch.IntTensor, optional): Ground truth durations. Defaults to None.

        Returns:
            Tuple[torch.FloatTensor, torch.FloatTensor]: Energy embedding, energy prediction.

        Shapes:
            - o_en: :math:`(B, C, T_{en})`
            - x_mask: :math:`(B, 1, T_{en})`
            - pitch: :math:`(B, 1, T_{de})`
            - dr: :math:`(B, T_{en})`
        """
        o_energy = self.energy_predictor(o_en, x_mask)
        if energy is not None:
            avg_energy = average_over_durations(energy, dr)
            # Put the control over phonemes
            if (energy_control is not None):
                print('Realizando controle de energia: ', energy_control, avg_energy)
                avg_energy = avg_energy*energy_control
            o_energy_emb = self.energy_emb(avg_energy)
            return o_energy_emb, o_energy, avg_energy
        if(energy_control is not None):
            print('Energy changed by args!')
            o_energy = o_energy + energy_control
        if(energy_replace is not None):
            print('Energy replaced by args!')
            o_energy = energy_replace
        o_energy_emb = self.energy_emb(o_energy)
        return o_energy_emb, o_energy
    
    def _forward_aligner(
        self, x: torch.FloatTensor, y: torch.FloatTensor, x_mask: torch.IntTensor, y_mask: torch.IntTensor
    ) -> Tuple[torch.IntTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Aligner forward pass.

        1. Compute a mask to apply to the attention map.
        2. Run the alignment network.
        3. Apply MAS to compute the hard alignment map.
        4. Compute the durations from the hard alignment map.

        Args:
            x (torch.FloatTensor): Input sequence.
            y (torch.FloatTensor): Output sequence.
            x_mask (torch.IntTensor): Input sequence mask.
            y_mask (torch.IntTensor): Output sequence mask.

        Returns:
            Tuple[torch.IntTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
                Durations from the hard alignment map, soft alignment potentials, log scale alignment potentials,
                hard alignment map.

        Shapes:
            - x: :math:`[B, T_en, C_en]`
            - y: :math:`[B, T_de, C_de]`
            - x_mask: :math:`[B, 1, T_en]`
            - y_mask: :math:`[B, 1, T_de]`

            - o_alignment_dur: :math:`[B, T_en]`
            - alignment_soft: :math:`[B, T_en, T_de]`
            - alignment_logprob: :math:`[B, 1, T_de, T_en]`
            - alignment_mas: :math:`[B, T_en, T_de]`
        """
        attn_mask = torch.unsqueeze(x_mask, -1) * torch.unsqueeze(y_mask, 2)
        alignment_soft, alignment_logprob = self.aligner(y.transpose(1, 2), x.transpose(1, 2), x_mask, None)
        alignment_mas = maximum_path(
            alignment_soft.squeeze(1).transpose(1, 2).contiguous(), attn_mask.squeeze(1).contiguous()
        )
        o_alignment_dur = torch.sum(alignment_mas, -1).int()
        alignment_soft = alignment_soft.squeeze(1).transpose(1, 2)
        return o_alignment_dur, alignment_soft, alignment_logprob, alignment_mas

    def _set_speaker_input(self, aux_input: Dict):
        d_vectors = aux_input.get("d_vectors", None)
        speaker_ids = aux_input.get("speaker_ids", None)

        if d_vectors is not None and speaker_ids is not None:
            raise ValueError("[!] Cannot use d-vectors and speaker-ids together.")

        if speaker_ids is not None and not hasattr(self, "emb_g"):
            raise ValueError("[!] Cannot use speaker-ids without enabling speaker embedding.")

        g = speaker_ids if speaker_ids is not None else d_vectors
        return g

    def forward(
        self,
        x: torch.LongTensor,
        x_lengths: torch.LongTensor,
        y_lengths: torch.LongTensor,
        y: torch.FloatTensor = None,
        dr: torch.IntTensor = None,
        pitch: torch.FloatTensor = None,
        energy: torch.FloatTensor = None,
        aux_input: Dict = {"d_vectors": None, "speaker_ids": None},  # pylint: disable=unused-argument
    ) -> Dict:
        """Model's forward pass.

        Args:
            x (torch.LongTensor): Input character sequences.
            x_lengths (torch.LongTensor): Input sequence lengths.
            y_lengths (torch.LongTensor): Output sequnce lengths. Defaults to None.
            y (torch.FloatTensor): Spectrogram frames. Only used when the alignment network is on. Defaults to None.
            dr (torch.IntTensor): Character durations over the spectrogram frames. Only used when the alignment network is off. Defaults to None.
            pitch (torch.FloatTensor): Pitch values for each spectrogram frame. Only used when the pitch predictor is on. Defaults to None.
            aux_input (Dict): Auxiliary model inputs for multi-speaker training. Defaults to `{"d_vectors": 0, "speaker_ids": None}`.

        Shapes:
            - x: :math:`[B, T_max]`
            - x_lengths: :math:`[B]`
            - y_lengths: :math:`[B]`
            - y: :math:`[B, T_max2]`
            - dr: :math:`[B, T_max]`
            - g: :math:`[B, C]`
            - pitch: :math:`[B, 1, T]`
        """

        
        g = self._set_speaker_input(aux_input)
        # compute sequence masks
        y_mask = torch.unsqueeze(sequence_mask(y_lengths, None), 1).float()
        x_mask = torch.unsqueeze(sequence_mask(x_lengths, x.shape[1]), 1).float()
        # encoder pass
        o_en, x_mask, g, x_emb = self._forward_encoder(x, x_mask, g)

        if (self.config.model_args.use_pos_tagger):

            # Get Texts Batch
            texts = []
            for idx in range(x.shape[0]):
                text = sequence_to_text(sequence = x[idx, :].tolist(), tp = self.characters, add_blank=self.add_blank)
                text = text[:x_lengths[idx]]
                texts.append([text])

            # Make parallel prediction
            predictions = self.pos(texts)

            embeddings = []
            words = []
            phrase_embeddings = []
            entities = []

            for sentence_idx in range(len(predictions)):
                sen_embeddings = []
                sen_words = []
                sen_ent = []
                for token_idx in range(len(predictions[sentence_idx])):
                    
                    # Get class and words delimiter
                    token = predictions[sentence_idx][token_idx]
                    entity = token['entity']
                    word = token['word']
                    
                    # Class to ID
                    token_id = self.pos.model.config.label2id[entity]
                    
                    # ID to embedding
                    embedding = self.pos_embs(torch.IntTensor([int(token_id)])).unsqueeze(2)
                    
                    # Embedding to fine grained
                    embedding_fine = embedding.repeat(1,1,len(word)) 
                    
                    # Save results
                    sen_embeddings.append(embedding_fine)
                    sen_words.append(word)
                    sen_ent.append(entity)
                
                # Embeddings delimited by words
                embeddings.append(sen_embeddings)

                # Words with delimitations
                words.append(sen_words)
                entities.append(sen_ent)
                
                # Concat all embeddings of a sentence (execpt BOS char)
                p_embedding = torch.cat(sen_embeddings, dim = 2)[:,:,1:]
                
                # Applying padding for max len
                if (p_embedding.shape[2] < x.shape[1]):
                    p_embedding = torch.nn.functional.pad(p_embedding, (0, x.shape[1] - p_embedding.shape[2]), mode='constant', value=0)

                # Save
                phrase_embeddings.append(p_embedding)

            # Concat along batch dim    
            batch_pos_embs = torch.cat(phrase_embeddings, dim=0)

            # POS pass
            o_en = o_en + batch_pos_embs
        
        # duration predictor pass
        if self.args.detach_duration_predictor:
            o_dr_log = self.duration_predictor(o_en.detach(), x_mask)
        else:
            o_dr_log = self.duration_predictor(o_en, x_mask)
        o_dr = torch.clamp(torch.exp(o_dr_log) - 1, 0, self.max_duration)
        # generate attn mask from predicted durations
        o_attn = self.generate_attn(o_dr.squeeze(1), x_mask)
        # aligner
        o_alignment_dur = None
        alignment_soft = None
        alignment_logprob = None
        alignment_mas = None
        if self.use_aligner:
            o_alignment_dur, alignment_soft, alignment_logprob, alignment_mas = self._forward_aligner(
                x_emb, y, x_mask, y_mask
            )
            alignment_soft = alignment_soft.transpose(1, 2)
            alignment_mas = alignment_mas.transpose(1, 2)
            dr = o_alignment_dur
        # pitch predictor pass
        o_pitch = None
        avg_pitch = None
        if self.args.use_pitch:
            o_pitch_emb, o_pitch, avg_pitch = self._forward_pitch_predictor(o_en, x_mask, pitch, dr)
            o_en = o_en + o_pitch_emb

        # ENERGY PREDICTOR PASS
        o_energy = None
        avg_energy = None
        if self.args.use_energy:
            o_energy_emb, o_energy, avg_energy = self._forward_energy_predictor(o_en, x_mask, energy, dr)
            o_en = o_en + o_energy_emb

        # decoder pass
        o_de, attn = self._forward_decoder(
            o_en, dr, x_mask, y_lengths, g=None
        )  # TODO: maybe pass speaker embedding (g) too
        outputs = {
            "model_outputs": o_de,  # [B, T, C]
            "durations_log": o_dr_log.squeeze(1),  # [B, T]
            "durations": o_dr.squeeze(1),  # [B, T]
            "attn_durations": o_attn,  # for visualization [B, T_en, T_de']
            "pitch_avg": o_pitch,
            "pitch_avg_gt": avg_pitch,
            "energy_avg": o_energy,
            "energy_avg_gt": avg_energy,
            "alignments": attn,  # [B, T_de, T_en]
            "alignment_soft": alignment_soft,
            "alignment_mas": alignment_mas,
            "o_alignment_dur": o_alignment_dur,
            "alignment_logprob": alignment_logprob,
            "x_mask": x_mask,
            "y_mask": y_mask,
        }
        return outputs

    @torch.no_grad()
    def inference(self, x, aux_input={"d_vectors": None, "speaker_ids": None, 'pitch_control': None}):  # pylint: disable=unused-argument
        """Model's inference pass.

        Args:
            x (torch.LongTensor): Input character sequence.
            aux_input (Dict): Auxiliary model inputs. Defaults to `{"d_vectors": None, "speaker_ids": None}`.

        Shapes:
            - x: [B, T_max]
            - x_lengths: [B]
            - g: [B, C]
        """
        g = self._set_speaker_input(aux_input)
        x_lengths = torch.tensor(x.shape[1:2]).to(x.device)
        x_mask = torch.unsqueeze(sequence_mask(x_lengths, x.shape[1]), 1).to(x.dtype).float()
        
        # encoder pass
        o_en, x_mask, g, _ = self._forward_encoder(x, x_mask, g)
        
        # pos tagger pass
        if (self.config.model_args.use_pos_tagger):

            # Get Texts Batch
            texts = []
            for idx in range(x.shape[0]):
                text = sequence_to_text(sequence = x[idx, :].tolist(), tp = self.characters, add_blank=self.add_blank)
                text = text[:x_lengths[idx]]
                texts.append([text])

            # Make parallel prediction
            predictions = self.pos(texts)

            embeddings = []
            words = []
            phrase_embeddings = []
            entities = []

            for sentence_idx in range(len(predictions)):
                sen_embeddings = []
                sen_words = []
                sen_ent = []
                for token_idx in range(len(predictions[sentence_idx])):
                    
                    # Get class and words delimiter
                    token = predictions[sentence_idx][token_idx]
                    entity = token['entity']
                    word = token['word']
                    
                    # Class to ID
                    token_id = self.pos.model.config.label2id[entity]
                    
                    # ID to embedding
                    embedding = self.pos_embs(torch.IntTensor([int(token_id)])).unsqueeze(2)
                    
                    # Embedding to fine grained
                    embedding_fine = embedding.repeat(1,1,len(word)) 
                    
                    # Save results
                    sen_embeddings.append(embedding_fine)
                    sen_words.append(word)
                    sen_ent.append(entity)
                
                # Embeddings delimited by words
                embeddings.append(sen_embeddings)

                # Words with delimitations
                words.append(sen_words)
                entities.append(sen_ent)
                
                # Concat all embeddings of a sentence (execpt BOS char)
                p_embedding = torch.cat(sen_embeddings, dim = 2)[:,:,1:]
                
                # Applying padding for max len
                if (p_embedding.shape[2] < x.shape[1]):
                    p_embedding = torch.nn.functional.pad(p_embedding, (0, x.shape[1] - p_embedding.shape[2]), mode='constant', value=0)

                # Save
                phrase_embeddings.append(p_embedding)

            # Concat along batch dim    
            batch_pos_embs = torch.cat(phrase_embeddings, dim=0)

            # POS pass
            o_en = o_en + batch_pos_embs
        
        # duration predictor pass
        o_dr_log = self.duration_predictor(o_en, x_mask)
        o_dr = self.format_durations(o_dr_log, x_mask).squeeze(1)
        y_lengths = o_dr.sum(1)

        # PITCH PREDICTOR PASS
        o_pitch = None
        if self.args.use_pitch:
            o_pitch_emb, o_pitch = self._forward_pitch_predictor(o_en, x_mask, pitch_control = aux_input['pitch_control'], pitch_replace=aux_input['pitch_replace'])
            o_en = o_en + o_pitch_emb
        
        # ENERGY PREDICTOR PASS
        o_energy = None
        if self.args.use_energy:
            o_energy_emb, o_energy = self._forward_energy_predictor(o_en, x_mask, energy_control = aux_input['energy_control'], energy_replace=aux_input['energy_replace'])
            o_en = o_en + o_energy_emb

        # decoder pass
        o_de, attn = self._forward_decoder(o_en, o_dr, x_mask, y_lengths, g=None)
        outputs = {
            "model_outputs": o_de,
            "alignments": attn,
            "pitch": o_pitch,
            "energy": o_energy,
            "durations_log": o_dr_log,
        }
        return outputs

    def train_step(self, batch: dict, criterion: nn.Module, step):
        text_input = batch["text_input"]
        text_lengths = batch["text_lengths"]
        mel_input = batch["mel_input"]
        mel_lengths = batch["mel_lengths"]
        pitch = batch["pitch"] if self.args.use_pitch else None
        energy = batch["energy"] if self.args.use_energy else None
        d_vectors = batch["d_vectors"]
        speaker_ids = batch["speaker_ids"]
        durations = batch["durations"]
        aux_input = {"d_vectors": d_vectors, "speaker_ids": speaker_ids}

        # forward pass
        outputs = self.forward(
            text_input, text_lengths, mel_lengths, y=mel_input, dr=durations, pitch=pitch, energy=energy, aux_input=aux_input
        )
        # use aligner's output as the duration target
        if self.use_aligner:
            durations = outputs["o_alignment_dur"]
        # use float32 in AMP
        with autocast(enabled=False):
            # compute loss
            loss_dict = criterion(
                decoder_output=outputs["model_outputs"],
                decoder_target=mel_input,
                decoder_output_lens=mel_lengths,
                dur_output=outputs["durations_log"],
                dur_target=durations,
                pitch_output=outputs["pitch_avg"] if self.use_pitch else None,
                pitch_target=outputs["pitch_avg_gt"] if self.use_pitch else None,
                energy_output=outputs["energy_avg"] if self.use_energy else None,
                energy_target=outputs["energy_avg_gt"] if self.use_energy else None,
                input_lens=text_lengths,
                alignment_logprob=outputs["alignment_logprob"] if self.use_aligner else None,
                alignment_soft=outputs["alignment_soft"] if self.use_binary_alignment_loss else None,
                alignment_hard=outputs["alignment_mas"] if self.use_binary_alignment_loss else None,
            )
            # compute duration error
            durations_pred = outputs["durations"]
            duration_error = torch.abs(durations - durations_pred).sum() / text_lengths.sum()
            loss_dict["duration_error"] = duration_error

        return outputs, loss_dict

    def _create_logs(self, batch, outputs, ap):
        """Create common logger outputs."""
        model_outputs = outputs["model_outputs"]
        alignments = outputs["alignments"]
        mel_input = batch["mel_input"]

        pred_spec = model_outputs[0].data.cpu().numpy()
        gt_spec = mel_input[0].data.cpu().numpy()
        align_img = alignments[0].data.cpu().numpy()

        figures = {
            "prediction": plot_spectrogram(pred_spec, ap, output_fig=False),
            "ground_truth": plot_spectrogram(gt_spec, ap, output_fig=False),
            "alignment": plot_alignment(align_img, output_fig=False),
        }

        # plot pitch figures
        if self.args.use_pitch:
            pitch = batch["pitch"]
            pitch_avg_expanded, _ = self.expand_encoder_outputs(
                outputs["pitch_avg"], outputs["durations"], outputs["x_mask"], outputs["y_mask"]
            )
            pitch = pitch[0, 0].data.cpu().numpy()
            # TODO: denormalize before plotting
            pitch = abs(pitch)
            pitch_avg_expanded = abs(pitch_avg_expanded[0, 0]).data.cpu().numpy()
            pitch_figures = {
                "pitch_ground_truth": plot_pitch(pitch, gt_spec, ap, output_fig=False),
                "pitch_avg_predicted": plot_pitch(pitch_avg_expanded, pred_spec, ap, output_fig=False),
            }
            figures.update(pitch_figures)

        # plot energy figures
        if self.args.use_energy:
            energy = batch["energy"]
            energy_avg_expanded, _ = self.expand_encoder_outputs(
                outputs["energy_avg"], outputs["durations"], outputs["x_mask"], outputs["y_mask"]
            )
            energy = energy[0, 0].data.cpu().numpy()
            energy = abs(energy)
            energy_avg_expanded = abs(energy_avg_expanded[0, 0]).data.cpu().numpy()
            energy_figures = {
                "energy_ground_truth": plot_pitch(energy, gt_spec, ap, output_fig=False),
                "energy_avg_predicted": plot_pitch(energy_avg_expanded, pred_spec, ap, output_fig=False),
            }
            figures.update(energy_figures)

        # plot the attention mask computed from the predicted durations
        if "attn_durations" in outputs:
            alignments_hat = outputs["attn_durations"][0].data.cpu().numpy()
            figures["alignment_hat"] = plot_alignment(alignments_hat.T, output_fig=False)

        # Sample audio
        train_audio = ap.inv_melspectrogram(pred_spec.T)
        return figures, {"audio": train_audio}

    def train_log(
        self, batch: dict, outputs: dict, logger: "Logger", assets: dict, steps: int
    ) -> None:  # pylint: disable=no-self-use
        ap = assets["audio_processor"]
        figures, audios = self._create_logs(batch, outputs, ap)
        logger.train_figures(steps, figures)
        logger.train_audios(steps, audios, ap.sample_rate)

    def eval_step(self, batch: dict, criterion: nn.Module, step):
        return self.train_step(batch, criterion, step)

    def eval_log(self, batch: dict, outputs: dict, logger: "Logger", assets: dict, steps: int) -> None:
        ap = assets["audio_processor"]
        figures, audios = self._create_logs(batch, outputs, ap)
        logger.eval_figures(steps, figures)
        logger.eval_audios(steps, audios, ap.sample_rate)

    def load_checkpoint(
        self, config, checkpoint_path, eval=False
    ):  # pylint: disable=unused-argument, redefined-builtin
        state = torch.load(checkpoint_path, map_location=torch.device("cpu"))
        self.load_state_dict(state["model"])
        if eval:
            self.eval()
            assert not self.training

    def get_criterion(self):
        from TTS.tts.layers.losses import ForwardTTSLoss  # pylint: disable=import-outside-toplevel

        return ForwardTTSLoss(self.config)

    def on_train_step_start(self, trainer):
        """Enable binary alignment loss when needed"""
        if trainer.total_steps_done > self.config.binary_align_loss_start_step:
            self.use_binary_alignment_loss = True
