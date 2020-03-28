import tensorflow as tf
from tensorflow.keras.applications.vgg16 import VGG16, preprocess_input
from tensorflow.keras.layers import Layer, Input, Conv2D, MaxPool2D, Activation
from tensorflow.keras.models import Model, Sequential
from tensorflow.keras.regularizers import l2
import helpers
import numpy as np
import math

def loc_loss_fn(actual_bbox_deltas, pred_bbox_deltas):
    """Calculating SSD localization loss value for only positive samples.
    inputs:
        actual_bbox_deltas = (batch_size, total_prior_boxes, [delta_y, delta_x, delta_h, delta_w])
        pred_bbox_deltas = (batch_size, total_prior_boxes, [delta_y, delta_x, delta_h, delta_w])

    outputs:
        loc_loss = localization / regression / bounding box loss value
    """
    pos_cond = tf.reduce_any(tf.not_equal(actual_bbox_deltas, 0), axis=2)
    pos_bbox_indices = tf.where(pos_cond)
    pos_bbox_count = tf.math.count_nonzero(pos_cond, axis=1)
    #
    total_pos_bboxes = tf.cast(tf.reduce_sum(pos_bbox_count), tf.float32)
    # Localization / Bbox loss calculation for pos bounding boxes
    y_true_bbox = tf.gather_nd(actual_bbox_deltas, pos_bbox_indices)
    y_pred_bbox = tf.gather_nd(pred_bbox_deltas, pos_bbox_indices)
    loc_loss_fn = tf.losses.Huber(reduction=tf.losses.Reduction.SUM)
    loc_loss = loc_loss_fn(y_true_bbox, y_pred_bbox)
    #
    loc_loss = tf.where(tf.not_equal(total_pos_bboxes, 0), loc_loss / total_pos_bboxes, 0.0)
    return loc_loss

def conf_loss_fn(actual_labels, pred_labels):
    """Calculating SSD confidence loss value by performing hard negative mining as mentioned in the paper.
    inputs:
        actual_labels = (batch_size, total_prior_boxes, total_labels)
        pred_labels = (batch_size, total_prior_boxes, total_labels)

    outputs:
        conf_loss = confidence / class / label loss value
    """
    # Confidence / Label loss calculation for all labels
    conf_loss_fn = tf.losses.CategoricalCrossentropy(reduction=tf.losses.Reduction.NONE)
    conf_loss_for_all = conf_loss_fn(actual_labels, pred_labels)
    #
    pos_cond = tf.not_equal(actual_labels[..., -1], 1)
    pos_bbox_indices = tf.where(pos_cond)
    pos_bbox_count = tf.math.count_nonzero(pos_cond, axis=1)
    # Hard negative mining
    neg_bbox_indices_count = tf.cast(pos_bbox_count * 3, tf.int32)
    # Remove positive index values from negative calculations
    masked_loss = tf.where(pos_cond, float("-inf"), conf_loss_for_all)
    # Sort loss values in descending order
    sorted_loss = tf.cast(tf.argsort(masked_loss, direction="DESCENDING"), tf.int64)
    batch_size, total_items = tf.shape(sorted_loss)[0], tf.shape(sorted_loss)[1]
    sorted_loss_indices = tf.tile([tf.range(total_items)], (batch_size, 1))
    neg_cond = sorted_loss_indices < tf.expand_dims(neg_bbox_indices_count, 1)
    neg_bbox_indices = tf.stack([tf.where(neg_cond)[:,0], sorted_loss[neg_cond]], 1)
    #
    total_pos_bboxes = tf.cast(tf.reduce_sum(pos_bbox_count), tf.float32)
    # Confidence / Label loss calculation for pos + neg bounding boxes
    selected_indices = tf.concat([pos_bbox_indices, neg_bbox_indices], 0)
    conf_loss = tf.reduce_sum(tf.gather_nd(conf_loss_for_all, selected_indices))
    #
    conf_loss = tf.where(tf.not_equal(total_pos_bboxes, 0), conf_loss / total_pos_bboxes, 0.0)
    return conf_loss

class L2Normalization(Layer):
    """Normalizing different scale features for fusion.
    paper: https://arxiv.org/abs/1506.04579
    inputs:
        feature_map = (batch_size, feature_map_height, feature_map_width, depth)

    outputs:
        normalized_feature_map = (batch_size, feature_map_height, feature_map_width, depth)
    """
    def __init__(self, scale_factor, **kwargs):
        super(L2Normalization, self).__init__(**kwargs)
        self.scale_factor = scale_factor

    def get_config(self):
        config = super(L2Normalization, self).get_config()
        config.update({"scale_factor": self.scale_factor})
        return config

    def build(self, input_shape):
        # Network need to learn scale factor for each channel
        init_scale_factor = tf.fill((input_shape[-1],), float(self.scale_factor))
        self.scale = tf.Variable(init_scale_factor, trainable=True)

    def call(self, inputs):
        return tf.nn.l2_normalize(inputs, axis=-1) * self.scale

class HeadWrapper(Layer):
    """Merging all feature maps for detections.
    inputs:
        conv4_3 = (batch_size, (layer_shape x aspect_ratios), last_dimension)
            ssd300 conv4_3 shape => (38 x 38 x 4) = 5776
        conv7 = (batch_size, (layer_shape x aspect_ratios), last_dimension)
            ssd300 conv7 shape => (19 x 19 x 6) = 2166
        conv8_2 = (batch_size, (layer_shape x aspect_ratios), last_dimension)
            ssd300 conv8_2 shape => (10 x 10 x 6) = 600
        conv9_2 = (batch_size, (layer_shape x aspect_ratios), last_dimension)
            ssd300 conv9_2 shape => (5 x 5 x 6) = 150
        conv10_2 = (batch_size, (layer_shape x aspect_ratios), last_dimension)
            ssd300 conv10_2 shape => (3 x 3 x 4) = 36
        conv11_2 = (batch_size, (layer_shape x aspect_ratios), last_dimension)
            ssd300 conv11_2 shape => (1 x 1 x 4) = 4
                                           Total = 8732 default box

    outputs:
        merged_head = (batch_size, total_prior_boxes, last_dimension)
    """

    def __init__(self, last_dimension, **kwargs):
        super(HeadWrapper, self).__init__(**kwargs)
        self.last_dimension = last_dimension

    def get_config(self):
        config = super(HeadWrapper, self).get_config()
        config.update({"last_dimension": self.last_dimension})
        return config

    def call(self, inputs):
        last_dimension = self.last_dimension
        batch_size = tf.shape(inputs[0])[0]
        outputs = []
        for conv_layer in inputs:
            outputs.append(tf.reshape(conv_layer, (batch_size, -1, last_dimension)))
        #
        return tf.concat(outputs, axis=1)

def get_model(hyper_params):
    """Generating ssd model for hyper params.
    inputs:
        hyper_params = dictionary

    outputs:
        ssd_model = tf.keras.model
    """
    scale_factor = 20.0
    reg_factor = 2e-4
    total_labels = hyper_params["total_labels"]
    # +1 for ratio 1
    len_aspect_ratios = [len(x) + 1 for x in hyper_params["aspect_ratios"]]
    #
    input = Input(shape=(None, None, 3), name="input")
    # conv1 block
    conv1_1 = Conv2D(64, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv1_1")(input)
    conv1_2 = Conv2D(64, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv1_2")(conv1_1)
    pool1 = MaxPool2D((2, 2), strides=(2, 2), padding="same", name="pool1")(conv1_2)
    # conv2 block
    conv2_1 = Conv2D(128, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv2_1")(pool1)
    conv2_2 = Conv2D(128, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv2_2")(conv2_1)
    pool2 = MaxPool2D((2, 2), strides=(2, 2), padding="same", name="pool2")(conv2_2)
    # conv3 block
    conv3_1 = Conv2D(256, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv3_1")(pool2)
    conv3_2 = Conv2D(256, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv3_2")(conv3_1)
    conv3_3 = Conv2D(256, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv3_3")(conv3_2)
    pool3 = MaxPool2D((2, 2), strides=(2, 2), padding="same", name="pool3")(conv3_3)
    # conv4 block
    conv4_1 = Conv2D(512, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv4_1")(pool3)
    conv4_2 = Conv2D(512, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv4_2")(conv4_1)
    conv4_3 = Conv2D(512, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv4_3")(conv4_2)
    pool4 = MaxPool2D((2, 2), strides=(2, 2), padding="same", name="pool4")(conv4_3)
    # conv5 block
    conv5_1 = Conv2D(512, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv5_1")(pool4)
    conv5_2 = Conv2D(512, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv5_2")(conv5_1)
    conv5_3 = Conv2D(512, (3, 3), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv5_3")(conv5_2)
    pool5 = MaxPool2D((3, 3), strides=(1, 1), padding="same", name="pool5")(conv5_3)
    # conv6 and conv7 converted from fc6 and fc7 and remove dropouts
    # These layers coming from modified vgg16 model
    # https://gist.github.com/weiliu89/2ed6e13bfd5b57cf81d6
    conv6 = Conv2D(1024, (3, 3), dilation_rate=6, padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv6")(pool5)
    conv7 = Conv2D(1024, (1, 1), strides=(1, 1), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv7")(conv6)
    ############################ Extra Feature Layers Start ############################
    # conv8 block <=> conv6 block in paper caffe implementation
    conv8_1 = Conv2D(256, (1, 1), strides=(1, 1), padding="valid", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv8_1")(conv7)
    conv8_2 = Conv2D(512, (3, 3), strides=(2, 2), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv8_2")(conv8_1)
    # conv9 block <=> conv7 block in paper caffe implementation
    conv9_1 = Conv2D(128, (1, 1), strides=(1, 1), padding="valid", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv9_1")(conv8_2)
    conv9_2 = Conv2D(256, (3, 3), strides=(2, 2), padding="same", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv9_2")(conv9_1)
    # conv10 block <=> conv8 block in paper caffe implementation
    conv10_1 = Conv2D(128, (1, 1), strides=(1, 1), padding="valid", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv10_1")(conv9_2)
    conv10_2 = Conv2D(256, (3, 3), strides=(1, 1), padding="valid", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv10_2")(conv10_1)
    # conv11 block <=> conv9 block in paper caffe implementation
    conv11_1 = Conv2D(128, (1, 1), strides=(1, 1), padding="valid", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv11_1")(conv10_2)
    conv11_2 = Conv2D(256, (3, 3), strides=(1, 1), padding="valid", activation="relu", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv11_2")(conv11_1)
    ############################ Extra Feature Layers End ############################
    # l2 normalization for each location in the feature map
    conv4_3_norm = L2Normalization(scale_factor)(conv4_3)
    #
    conv4_3_labels = Conv2D(len_aspect_ratios[0] * total_labels, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv4_3_label_output")(conv4_3_norm)
    conv7_labels = Conv2D(len_aspect_ratios[1] * total_labels, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv7_label_output")(conv7)
    conv8_2_labels = Conv2D(len_aspect_ratios[2] * total_labels, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv8_2_label_output")(conv8_2)
    conv9_2_labels = Conv2D(len_aspect_ratios[3] * total_labels, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv9_2_label_output")(conv9_2)
    conv10_2_labels = Conv2D(len_aspect_ratios[4] * total_labels, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv10_2_label_output")(conv10_2)
    conv11_2_labels = Conv2D(len_aspect_ratios[5] * total_labels, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv11_2_label_output")(conv11_2)
    #
    conv4_3_boxes = Conv2D(len_aspect_ratios[0] * 4, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv4_3_boxes_output")(conv4_3_norm)
    conv7_boxes = Conv2D(len_aspect_ratios[1] * 4, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv7_boxes_output")(conv7)
    conv8_2_boxes = Conv2D(len_aspect_ratios[2] * 4, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv8_2_boxes_output")(conv8_2)
    conv9_2_boxes = Conv2D(len_aspect_ratios[3] * 4, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv9_2_boxes_output")(conv9_2)
    conv10_2_boxes = Conv2D(len_aspect_ratios[4] * 4, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv10_2_boxes_output")(conv10_2)
    conv11_2_boxes = Conv2D(len_aspect_ratios[5] * 4, (3, 3), padding="same", kernel_initializer="glorot_normal", kernel_regularizer=l2(reg_factor), name="conv11_2_boxes_output")(conv11_2)
    #
    pred_labels = HeadWrapper(total_labels, name="labels_head")([conv4_3_labels, conv7_labels, conv8_2_labels,
                                                                 conv9_2_labels, conv10_2_labels, conv11_2_labels])
    pred_labels = Activation("softmax", name="conf")(pred_labels)
    #
    pred_bbox_deltas = HeadWrapper(4, name="loc")([conv4_3_boxes, conv7_boxes, conv8_2_boxes,
                                                   conv9_2_boxes, conv10_2_boxes, conv11_2_boxes])
    #
    return Model(inputs=input, outputs=[pred_bbox_deltas, pred_labels])

def init_model(model):
    """Initializing model with dummy data for load weights with optimizer state and also graph construction.
    inputs:
        model = tf.keras.model

    """
    model(tf.random.uniform((1, 512, 512, 3)))

def get_scale_for_nth_feature_map(k, m=6, scale_min=0.2, scale_max=0.9):
    """Calculating scale value for nth feature map using the given method in the paper.
    inputs:
        aspect_ratios = for all default box shapes + 1 for ratio 1
        feature_map_index = nth feature map for scale calculation
        total_feature_map = length of all using feature map for detections 6 for ssd300

    outputs:
        height_width_pairs = [(height1, width1), ..., (heightN, widthN)]
    """
    return scale_min + ((scale_max - scale_min) / (m - 1)) * (k - 1)

def get_height_width_pairs(aspect_ratios, feature_map_index, total_feature_map):
    """Generating height and width pairs for different aspect ratios and feature map shapes.
    inputs:
        aspect_ratios = for all feature map shapes + 1 for ratio 1
        feature_map_index = nth feature map for scale calculation
        total_feature_map = length of all using feature map for detections 6 for ssd300

    outputs:
        height_width_pairs = [(height1, width1), ..., (heightN, widthN)]
    """
    current_scale = get_scale_for_nth_feature_map(feature_map_index, m=total_feature_map)
    next_scale = get_scale_for_nth_feature_map(feature_map_index + 1, m=total_feature_map)
    height_width_pairs = []
    for aspect_ratio in aspect_ratios:
        height = current_scale / math.sqrt(aspect_ratio)
        width = current_scale * math.sqrt(aspect_ratio)
        height_width_pairs.append((height, width))
    # 1 extra pair for ratio 1
    height = width = math.sqrt(current_scale * next_scale)
    height_width_pairs.append((height, width))
    return height_width_pairs

def generate_base_prior_boxes(stride, height_width_pairs):
    """Generating top left prior boxes for given stride, height and width pairs of different aspect ratios.
    These prior boxes same with the anchors in Faster-RCNN.
    inputs:
        stride = step size
        height_width_pairs = [(height1, width1), ..., (heightN, widthN)]

    outputs:
        base_prior_boxes = (prior_box_count, [y1, x1, y2, x2])
    """
    center = stride / 2
    base_prior_boxes = []
    for height_width in height_width_pairs:
        height, width = height_width
        x_min = center - width / 2
        y_min = center - height / 2
        x_max = center + width / 2
        y_max = center + height / 2
        base_prior_boxes.append([y_min, x_min, y_max, x_max])
    return np.array(base_prior_boxes, dtype=np.float32)

def generate_prior_boxes(feature_map_shapes, aspect_ratios):
    """Generating top left prior boxes for given stride, height and width pairs of different aspect ratios.
    These prior boxes same with the anchors in Faster-RCNN.
    inputs:
        feature_map_shapes = for all feature map output size
        aspect_ratios = for all feature map shapes + 1 for ratio 1

    outputs:
        prior_boxes = (total_prior_boxes, [y1, x1, y2, x2])
            these values in normalized format between [0, 1]
    """
    prior_boxes = []
    for i, feature_map_shape in enumerate(feature_map_shapes):
        prior_box_count = len(aspect_ratios[i]) + 1
        height_width_pairs = get_height_width_pairs(aspect_ratios[i], i+1, len(feature_map_shapes))
        base_prior_boxes = generate_base_prior_boxes(1. / feature_map_shape, height_width_pairs)
        #
        grid_coords = np.arange(0, feature_map_shape)
        #
        grid_x, grid_y = np.meshgrid(grid_coords, grid_coords)
        grid_map = np.vstack((grid_y.ravel(), grid_x.ravel(), grid_y.ravel(), grid_x.ravel())).transpose()
        grid_map = grid_map / feature_map_shape
        #
        output_area = feature_map_shape ** 2
        prior_boxes_for_feature_map = base_prior_boxes.reshape((1, prior_box_count, 4)) + \
                                      grid_map.reshape((1, output_area, 4)).transpose((1, 0, 2))
        prior_boxes_for_feature_map = prior_boxes_for_feature_map.reshape((output_area * prior_box_count, 4)).astype(np.float32)
        prior_boxes.append(prior_boxes_for_feature_map)
    prior_boxes = np.concatenate(prior_boxes, axis=0)
    return np.clip(prior_boxes, 0, 1)

def generator(dataset, prior_boxes, hyper_params):
    """Tensorflow data generator for fit method, yielding inputs and outputs.
    inputs:
        dataset = tf.data.Dataset, PaddedBatchDataset
        prior_boxes = (total_prior_boxes, [y1, x1, y2, x2])
            these values in normalized format between [0, 1]
        hyper_params = dictionary

    outputs:
        yield inputs, outputs
    """
    while True:
        for image_data in dataset:
            img, gt_boxes, gt_labels = image_data
            input_img = preprocess_input(img)
            input_img = tf.image.convert_image_dtype(input_img, tf.float32)
            actual_bbox_deltas, actual_labels = calculate_actual_outputs(prior_boxes, gt_boxes, gt_labels, hyper_params)
            yield input_img, (actual_bbox_deltas, actual_labels)

def calculate_actual_outputs(prior_boxes, gt_boxes, gt_labels, hyper_params):
    """Calculate ssd actual output values.
    Batch operations supported.
    inputs:
        prior_boxes = (total_prior_boxes, [y1, x1, y2, x2])
            these values in normalized format between [0, 1]
        gt_boxes (batch_size, gt_box_size, [y1, x1, y2, x2])
            these values in normalized format between [0, 1]
        gt_labels (batch_size, gt_box_size)
        hyper_params = dictionary

    outputs:
        bbox_deltas = (batch_size, total_bboxes, [delta_y, delta_x, delta_h, delta_w])
        bbox_labels = (batch_size, total_bboxes, [0,0,...,0])
    """
    batch_size = tf.shape(gt_boxes)[0]
    total_labels = hyper_params["total_labels"]
    iou_threshold = hyper_params["iou_threshold"]
    variances = hyper_params["variances"]
    total_prior_boxes = prior_boxes.shape[0]
    pos_bbox_indices, gt_box_indices = helpers.get_selected_indices(prior_boxes, gt_boxes, iou_threshold)
    #
    gt_boxes_map = tf.gather_nd(gt_boxes, gt_box_indices)
    expanded_gt_boxes = tf.scatter_nd(pos_bbox_indices, gt_boxes_map, (batch_size, total_prior_boxes, 4))
    bbox_deltas = helpers.get_deltas_from_bboxes(prior_boxes, expanded_gt_boxes) / variances
    #
    pos_gt_labels_map = tf.gather_nd(gt_labels, gt_box_indices)
    pos_gt_labels_map = tf.one_hot(pos_gt_labels_map, total_labels)
    bbox_labels = tf.fill((batch_size, total_prior_boxes), total_labels-1)
    bbox_labels = tf.one_hot(bbox_labels, total_labels)
    bbox_labels = tf.tensor_scatter_nd_update(bbox_labels, pos_bbox_indices, pos_gt_labels_map)
    #
    return bbox_deltas, bbox_labels
