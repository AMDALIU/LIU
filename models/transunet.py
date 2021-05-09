from data_processing.data_parser import N_CLASSES
import models.encoder_layers as encoder_layers
import models.decoder_layers as decoder_layers
import tensorflow_addons as tfa
import models.utils as utils
from medpy import metric
import tensorflow as tf
import math
tfk = tf.keras
tfkl = tfk.layers
tfm = tf.math
tfkc = tfk.callbacks
N_CLASSES = 9
MODELS_URL = 'https://storage.googleapis.com/vit_models/imagenet21k/'

class TransUnet():
    def __init__(self, config):
        self.config = config 
        self.image_size = config.image_size
        self.patch_size = config.patch_size
        self.n_layers = config.n_layers
        self.hidden_size = config.hidden_size
        self.n_heads = config.n_heads
        self.name = config.name
        self.mlp_dim = config.mlp_dim
        self.dropout = config.dropout
        self.filters = config.filters
        self.kernel_size = config.kernel_size
        self.upsampling_factor = config.upsampling_factor
        self.hybrid = config.hybrid
        self.model = self.build_model()


    def build_model(self):
        # Tranformer Encoder
        assert self.image_size % self.patch_size == 0, "image_size must be a multiple of patch_size"
        x = tf.keras.layers.Input(shape=(self.image_size, self.image_size, 3))
        
        ## Embedding
        if self.hybrid:
            grid_size = self.config.grid
            self.patch_size = self.image_size // 16 // grid_size[0]
            if self.patch_size == 0:
                self.patch_size = 1 

            resnet50v2, features = self.resnet_embeddings(x)
            y = resnet50v2.get_layer("conv4_block6_preact_relu").output
            x = resnet50v2.input
        else:
            y = x
            features = None

        y = tf.keras.layers.Conv2D(
            filters=self.hidden_size,
            kernel_size=self.patch_size,
            strides=self.patch_size,
            padding="valid",
            name="embedding",
            trainable=False
        )(y)
        y = tf.keras.layers.Reshape(
            (y.shape[1] * y.shape[2], self.hidden_size))(y)
        y = encoder_layers.AddPositionEmbs(name="Transformer/posembed_input")(y)
        
        # Transformer/Encoder
        for n in range(self.n_layers):
            y, _ = encoder_layers.TransformerBlock(
                n_heads=self.n_heads,
                mlp_dim=self.mlp_dim,
                dropout=self.dropout,
                name=f"Transformer/encoderblock_{n}",
            )(y)
        y = tfkl.LayerNormalization(
            epsilon=1e-6, name="Transformer/encoder_norm"
        )(y)

        n_patch_sqrt = int(math.sqrt(y.shape[1]))
        
        y = tfkl.Reshape(
            target_shape=[n_patch_sqrt, n_patch_sqrt, self.hidden_size])(y)
        
        ## Decoder 
        if "decoder_channels" in self.config:
            y = decoder_layers.DecoderCup(
                decoder_channels=self.config.decoder_channels, n_skip=self.config.n_skip)(y, features)
        
        ## Segmentation Head
        y = decoder_layers.SegmentationHead(
            filters=self.filters, kernel_size=self.kernel_size, upsampling_factor=self.upsampling_factor)(y)

        return tfk.models.Model(inputs=x, outputs=y, name=self.name)

    def load_pretrained(self):
        """Load model weights for a known configuration."""
        origin = MODELS_URL + self.config.pretrained_filename
        fname = self.config.pretrained_filename
        local_filepath = tf.keras.utils.get_file(
            fname, origin, cache_subdir="weights")
            
        utils.load_weights_numpy(self.model, local_filepath)

    def compile(self):
        self.load_pretrained()
        
        optimizer = tfa.optimizers.SGDW(
            weight_decay=1e-4, momentum=.9, learning_rate=0.01)

        self.model.compile(optimizer=optimizer, loss=[TransUnet.segmentation_loss])

    @tf.function
    def segmentation_loss(y_true, y_pred):
        cross_entropy_loss = tf.losses.categorical_crossentropy(
            y_true=y_true, y_pred=y_pred, from_logits=True)
        dice_loss = TransUnet.gen_dice(y_true, y_pred)
        return 0.5 * cross_entropy_loss + 0.5 * dice_loss

    @tf.function
    def gen_dice(y_true, y_pred, eps=1e-6):
        """both tensors are [b, h, w, classes] and y_pred is in logit form"""

        # [b, h, w, classes]
        pred_tensor = tf.nn.softmax(y_pred)
        y_true_shape = tf.shape(y_true)

        # [b, h*w, classes]
        y_true = tf.reshape(
            y_true, [-1, y_true_shape[1]*y_true_shape[2], y_true_shape[3]])
        y_pred = tf.reshape(
            pred_tensor, [-1, y_true_shape[1]*y_true_shape[2], y_true_shape[3]])

        # [b, classes]
        # count how many of each class are present in
        # each image, if there are zero, then assign
        # them a fixed weight of eps
        counts = tf.reduce_sum(y_true, axis=1)
        weights = 1. / (counts ** 2)
        weights = tf.where(tf.math.is_finite(weights), weights, eps)

        multed = tf.reduce_sum(y_true * y_pred, axis=1)
        summed = tf.reduce_sum(y_true + y_pred, axis=1)

        # [b]
        numerators = tf.reduce_sum(weights*multed, axis=-1)
        denom = tf.reduce_sum(weights*summed, axis=-1)
        dices = 1. - 2. * numerators / denom
        dices = tf.where(tf.math.is_finite(dices), dices, tf.zeros_like(dices))
        return tf.reduce_mean(dices)


    def resnet_embeddings(self, x):
        resnet50v2 = tfk.applications.ResNet50V2(
            include_top=False, input_shape=(self.image_size, self.image_size, 3))
        resnet50v2.trainable = False
        _ = resnet50v2(x)
        layers = ["conv3_block4_preact_relu", 
                "conv2_block3_preact_relu",
                "conv1_conv"]

        features = []                        
        for l in layers:
            features.append(resnet50v2.get_layer(l).output)
        x = resnet50v2.get_layer("conv4_block6_preact_relu").output
        return resnet50v2, features

    def save_model(self, saved_model_path):
        save_options = tf.saved_model.SaveOptions(
            experimental_io_device='/job:localhost')
        self.model.save(saved_model_path, options=save_options)
    
    def load_model(self, tpu_strategy, saved_model_path):
        with tpu_strategy.scope():
            load_options = tf.saved_model.LoadOptions(experimental_io_device='/job:localhost')
            # model = tf.keras.models.load_model(saved_model_path, options=load_options, custom_objects={'loss': vit.TransUnet.segmentation_loss})
            model = tf.keras.models.load_model(saved_model_path, options=load_options, compile=False)
            self.model = model
            return model
    
    @staticmethod
    def calculate_metric_percase(pred, target):
        pred[pred > 0] = 1
        target[target > 0] = 1
        if pred.sum() > 0 and target.sum()>0:
            dice = metric.binary.dc(pred, target)
            hd95 = metric.binary.hd95(pred, target)
            return dice, hd95
        # elif pred.sum() > 0 and target.sum()==0:
        #     return 1, 0
        else:
            return 0, 0

    def evaluate(self, X, label):
        y_pred = self.model(X)
        y_pred = tf.math.argmax(tf.softmax(
            y_pred, dim=1), dim=1).squeeze(0)
        metric_list = []
        for i in range(1, N_CLASSES):
            metric_list.append(self.calculate_metric_percase(
                y_pred == i, label == i))
        return metric_list

        
    
