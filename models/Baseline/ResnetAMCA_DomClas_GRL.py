import os
repo_path = os.getenv('MMWAVE_PATH')
import sys
sys.path.append(os.path.join(repo_path, 'models'))
from utils import *
from resnet_amca import ResNetAMCA, AM_logits
import tensorflow as tf
import numpy as np
import argparse
import inspect
import shutil
import yaml
import h5py
from sklearn.metrics import confusion_matrix


def get_parser():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--epochs', type=int, default=1000)
    parser.add_argument('--init_lr', type=float, default=1e-3)
    parser.add_argument('--num_features', type=int, default=128)
    parser.add_argument('--model_filters', type=int, default=64)
    parser.add_argument('--activation_fn', default='selu')
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--num_classes', type=int, default=10)
    parser.add_argument('--train_src_days', type=int, default=3)
    parser.add_argument('--train_trg_days', type=int, default=0)
    parser.add_argument('--train_ser_days', type=int, default=3)
    parser.add_argument('--train_con_days', type=int, default=0)
    parser.add_argument('--save_freq', type=int, default=25)
    parser.add_argument('--log_images_freq', type=int, default=25)
    parser.add_argument('--checkpoint_path', default="checkpoints")
    parser.add_argument('--summary_writer_path', default="tensorboard_logs")
    parser.add_argument('--anneal', type=int, default=4)
    parser.add_argument('--s', type=int, default=10)
    parser.add_argument('--m', type=float, default=0.1)
    parser.add_argument('--ca', type=float, default=1e-3)
    parser.add_argument('--dm_lambda', type=float, default=1e-4)
    parser.add_argument('--log_dir', default="logs/Baselines/AMCA_DomClas/")
    parser.add_argument('--disc_hidden', type=int, default=128)
    parser.add_argument('--notes', default="AMCA_STDomClas_GRL_Baseline")
    return parser


def save_arg(arg):
    arg_dict = vars(arg)
    if not os.path.exists(arg.log_dir):
        os.makedirs(arg.log_dir)
    with open(os.path.join(arg.log_dir, "config.yaml"), 'w') as f:
        yaml.dump(arg_dict, f)


def get_cross_entropy_loss(labels, logits):
    loss = tf.nn.softmax_cross_entropy_with_logits(labels=labels,
                                                   logits=logits)
    return tf.reduce_mean(loss)


@tf.function
def test_step(images):
    logits, _ = model(images, training=False)
    return tf.nn.softmax(logits)


@tf.custom_gradient
def reverse_gradient(x, hp_lambda):
    def custom_grad(dy):
        return tf.math.multiply(tf.negative(dy), hp_lambda), None

    return x, custom_grad


class GradientReversal(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()

    def call(self, inputs, lambda_hp):
        return reverse_gradient(inputs, lambda_hp)


class ResNetAMCADomClas(ResNetAMCA):
    def __init__(self,
                 num_classes,
                 num_features,
                 num_filters=64,
                 activation='relu',
                 regularizer='batchnorm',
                 dropout_rate=0,
                 ca_decay=1e-3,
                 disc_hidden=128,
                 num_domains=4):
        super().__init__(num_classes, num_features, num_filters, activation,
                         regularizer, dropout_rate, ca_decay)

        self.rev_grad = GradientReversal()
        self.disc = []
        self.disc.append(
            tf.keras.layers.Dense(disc_hidden, activation=activation))
        self.disc.append(tf.keras.layers.Dense(num_domains,
                                               name='disc_logits'))

    def call(self, x, training=False, hp_lambda=0.0):
        x = self.conv1(x)
        x = self.bn1(x, training=training)
        x = self.act1(x)
        x = self.max_pool1(x)

        for block in self.blocks:
            x = block(x, training=training)

        x = self.avg_pool(x)

        fc1 = self.fc1(x)
        logits = self.logits(fc1)

        disc = self.rev_grad(x, hp_lambda)
        for dense in self.disc:
            disc = dense(disc)

        return logits, disc


@tf.function
def train_step(src_data,
               trg_data,
               srv_data,
               con_data,
               s,
               m,
               hp_lambda=0):
    src_images, src_labels = src_data
    trg_images, trg_labels = trg_data
    srv_images, srv_labels = srv_data
    con_images, con_labels = con_data

    with tf.GradientTape() as tape:
        src_logits, src_dom_logits = model(src_images,
                                           training=True,
                                           hp_lambda=hp_lambda)
        trg_logits, trg_dom_logits = model(trg_images,
                                           training=True,
                                           hp_lambda=hp_lambda)
        srv_logits, srv_dom_logits = model(srv_images,
                                           training=True,
                                           hp_lambda=hp_lambda)
        con_logits, con_dom_logits = model(con_images,
                                           training=True,
                                           hp_lambda=hp_lambda)

        src_logits = AM_logits(labels=src_labels, logits=src_logits, m=m, s=s)
        batch_cross_entropy_loss = get_cross_entropy_loss(labels=src_labels,
                                                          logits=src_logits)

        domain_labels = tf.concat([
            tf.one_hot(tf.zeros(batch_size, dtype=tf.uint8), 4),
            tf.one_hot(tf.ones(batch_size, dtype=tf.uint8), 4),
            tf.one_hot(tf.ones(batch_size, dtype=tf.uint8) * 2, 4),
            tf.one_hot(tf.ones(batch_size, dtype=tf.uint8) * 3, 4)
        ],
                                  axis=0)
        domain_logits = tf.concat(
            [src_dom_logits, trg_dom_logits, srv_dom_logits, con_dom_logits],
            axis=0)
        batch_domain_loss = get_cross_entropy_loss(labels=domain_labels,
                                                   logits=domain_logits)

        total_loss = batch_cross_entropy_loss + \
                     dm_lambda * batch_domain_loss

    gradients = tape.gradient(total_loss, model.trainable_variables)
    optimizer.apply_gradients(zip(gradients, model.trainable_variables))

    source_train_acc(src_labels, tf.nn.softmax(src_logits))
    cross_entropy_loss(batch_cross_entropy_loss)
    domain_loss(batch_domain_loss)


if __name__ == '__main__':
    parser = get_parser()
    arg = parser.parse_args()

    dataset_path = os.path.join(repo_path, 'data')
    num_classes = arg.num_classes
    batch_size = arg.batch_size
    train_src_days = arg.train_src_days
    train_ser_days = arg.train_ser_days
    train_con_days = arg.train_con_days
    train_trg_days = arg.train_trg_days
    save_freq = arg.save_freq
    epochs = arg.epochs
    init_lr = arg.init_lr
    num_features = arg.num_features
    activation_fn = arg.activation_fn
    model_filters = arg.model_filters
    anneal = arg.anneal
    disc_hidden = arg.disc_hidden
    s = arg.s
    m = arg.m
    ca = arg.ca
    log_images_freq = arg.log_images_freq
    dm_lambda = arg.dm_lambda

    num_domains = 0
    if train_ser_days > 0:
        num_domains += 1
    if train_con_days > 0:
        num_domains += 1
    if train_src_days > 0:
        num_domains += 1
    if train_trg_days > 0:
        num_domains += 1

    run_params = dict(vars(arg))
    del run_params['num_classes']
    del run_params['s']
    del run_params['anneal']
    del run_params['ca']
    del run_params['activation_fn']
    del run_params['log_images_freq']
    del run_params['log_dir']
    del run_params['checkpoint_path']
    del run_params['summary_writer_path']
    del run_params['save_freq']
    sorted(run_params)

    run_params = str(run_params).replace(" ",
                                         "").replace("'",
                                                     "").replace(",",
                                                                 "-")[1:-1]
    log_dir = os.path.join(repo_path, arg.log_dir, run_params)
    arg.log_dir = log_dir

    summary_writer_path = os.path.join(log_dir, arg.summary_writer_path)
    checkpoint_path = os.path.join(log_dir, arg.checkpoint_path)

    save_arg(arg)
    shutil.copy2(inspect.getfile(ResNetAMCA), arg.log_dir)
    shutil.copy2(os.path.abspath(__file__), arg.log_dir)
    '''
    Data Preprocessing
    '''
    X_data, y_data, classes = get_h5dataset(
        os.path.join(dataset_path, 'source_data.h5'))
    X_data, y_data = balance_dataset(X_data,
                                     y_data,
                                     num_days=10,
                                     num_classes=len(classes),
                                     max_samples_per_class=95)

    #split days of data to train and test
    X_src = X_data[y_data[:, 1] < train_src_days]
    y_src = y_data[y_data[:, 1] < train_src_days, 0]
    y_src = np.eye(len(classes))[y_src]
    X_train_src, X_test_src, y_train_src, y_test_src = train_test_split(
        X_src, y_src, stratify=y_src, test_size=0.10, random_state=42)

    X_trg = X_data[y_data[:, 1] >= train_src_days]
    y_trg = y_data[y_data[:, 1] >= train_src_days]
    X_train_trg = X_trg[y_trg[:, 1] < train_src_days + train_trg_days]
    y_train_trg = y_trg[y_trg[:, 1] < train_src_days + train_trg_days, 0]
    y_train_trg = np.eye(len(classes))[y_train_trg]

    X_test_trg = X_data[y_data[:, 1] >= train_src_days + train_trg_days]
    y_test_trg = y_data[y_data[:, 1] >= train_src_days + train_trg_days, 0]
    y_test_trg = np.eye(len(classes))[y_test_trg]

    del X_src, y_src, X_trg, y_trg, X_data, y_data

    #mean center and normalize dataset
    X_train_src, src_mean = mean_center(X_train_src)
    X_train_src, src_min, src_ptp = normalize(X_train_src)

    X_test_src, _ = mean_center(X_test_src, src_mean)
    X_test_src, _, _ = normalize(X_test_src, src_min, src_ptp)

    if (X_train_trg.shape[0] != 0):
        X_train_trg, trg_mean = mean_center(X_train_trg)
        X_train_trg, trg_min, trg_ptp = normalize(X_train_trg)

        X_test_trg, _ = mean_center(X_test_trg, trg_mean)
        X_test_trg, _, _ = normalize(X_test_trg, trg_min, trg_ptp)
    else:
        X_test_trg, _ = mean_center(X_test_trg, src_mean)
        X_test_trg, _, _ = normalize(X_test_trg, src_min, src_ptp)

    X_train_src = X_train_src.astype(np.float32)
    y_train_src = y_train_src.astype(np.uint8)
    X_test_src = X_test_src.astype(np.float32)
    y_test_src = y_test_src.astype(np.uint8)
    X_train_trg = X_train_trg.astype(np.float32)
    y_train_trg = y_train_trg.astype(np.uint8)
    X_test_trg = X_test_trg.astype(np.float32)
    y_test_trg = y_test_trg.astype(np.uint8)

    X_train_conf, y_train_conf, X_test_conf, y_test_conf = get_trg_data(
        os.path.join(dataset_path, 'target_conf_data.h5'), classes,
        train_con_days)
    X_train_server, y_train_server, X_test_server, y_test_server = get_trg_data(
        os.path.join(dataset_path, 'target_server_data.h5'), classes,
        train_ser_days)
    _, _, X_data_office, y_data_office = get_trg_data(os.path.join(
        dataset_path, 'target_office_data.h5'),
                                                      classes,
                                                      0,
                                                      test_all=True)

    print("Final shapes: ")
    print(" Train Src:   ", X_train_src.shape, y_train_src.shape, "\n",
          "Test Src:    ", X_test_src.shape, y_test_src.shape, "\n",
          "Train Trg:   ", X_train_trg.shape, y_train_trg.shape, "\n",
          "Test Trg:    ", X_test_trg.shape, y_test_trg.shape)
    print(" Train Conf:  ", X_train_conf.shape, y_train_conf.shape, "\n",
          "Test Conf:   ", X_test_conf.shape, y_test_conf.shape, "\n",
          "Train Server:", X_train_server.shape, y_train_server.shape, "\n",
          "Test Server: ", X_test_server.shape, y_test_server.shape, "\n",
          "Test office: ", X_data_office.shape, y_data_office.shape)

    #get tf.data objects for each set
    #Test
    conf_test_set = tf.data.Dataset.from_tensor_slices(
        (X_test_conf, y_test_conf))
    conf_test_set = conf_test_set.batch(batch_size, drop_remainder=False)
    conf_test_set = conf_test_set.prefetch(batch_size)

    server_test_set = tf.data.Dataset.from_tensor_slices(
        (X_test_server, y_test_server))
    server_test_set = server_test_set.batch(batch_size, drop_remainder=False)
    server_test_set = server_test_set.prefetch(batch_size)

    office_test_set = tf.data.Dataset.from_tensor_slices(
        (X_data_office, y_data_office))
    office_test_set = office_test_set.batch(batch_size, drop_remainder=False)
    office_test_set = office_test_set.prefetch(batch_size)

    src_test_set = tf.data.Dataset.from_tensor_slices((X_test_src, y_test_src))
    src_test_set = src_test_set.batch(batch_size, drop_remainder=False)
    src_test_set = src_test_set.prefetch(batch_size)

    time_test_set = tf.data.Dataset.from_tensor_slices(
        (X_test_trg, y_test_trg))
    time_test_set = time_test_set.batch(batch_size, drop_remainder=False)
    time_test_set = time_test_set.prefetch(batch_size)

    #Train
    train_datasets = []

    src_train_set = tf.data.Dataset.from_tensor_slices(
        (X_train_src, y_train_src))
    src_train_set = src_train_set.shuffle(X_train_src.shape[0])
    src_train_set = src_train_set.batch(batch_size, drop_remainder=True)
    src_train_set = src_train_set.prefetch(batch_size)
    train_datasets.append(src_train_set)

    if train_trg_days > 0:
        trg_train_set = tf.data.Dataset.from_tensor_slices(
            (X_train_trg, y_train_trg))
        trg_train_set = trg_train_set.shuffle(X_train_trg.shape[0])
        trg_train_set = trg_train_set.batch(batch_size, drop_remainder=True)
        trg_train_set = trg_train_set.prefetch(batch_size)
        train_datasets.append(trg_train_set)

    if train_ser_days > 0:
        ser_train_set = tf.data.Dataset.from_tensor_slices(
            (X_train_server, y_train_server))
        ser_train_set = ser_train_set.shuffle(X_train_server.shape[0])
        ser_train_set = ser_train_set.batch(batch_size, drop_remainder=True)
        ser_train_set = ser_train_set.prefetch(batch_size)
        train_datasets.append(ser_train_set)

    if train_con_days > 0:
        con_train_set = tf.data.Dataset.from_tensor_slices(
            (X_train_conf, y_train_conf))
        con_train_set = con_train_set.shuffle(X_train_conf.shape[0])
        con_train_set = con_train_set.batch(batch_size, drop_remainder=True)
        con_train_set = con_train_set.prefetch(batch_size)
        train_datasets.append(con_train_set)

    '''
    Tensorflow Model
    '''

    source_train_acc = tf.keras.metrics.CategoricalAccuracy()
    source_test_acc = tf.keras.metrics.CategoricalAccuracy()
    temporal_test_acc = tf.keras.metrics.CategoricalAccuracy()
    office_test_acc = tf.keras.metrics.CategoricalAccuracy()
    server_train_acc = tf.keras.metrics.CategoricalAccuracy()
    server_test_acc = tf.keras.metrics.CategoricalAccuracy()
    conference_train_acc = tf.keras.metrics.CategoricalAccuracy()
    conference_test_acc = tf.keras.metrics.CategoricalAccuracy()
    cross_entropy_loss = tf.keras.metrics.Mean()
    domain_loss = tf.keras.metrics.Mean()

    learning_rate = tf.keras.optimizers.schedules.PolynomialDecay(
        init_lr,
        decay_steps=(X_train_src.shape[0] // batch_size) * 200,
        end_learning_rate=init_lr * 1e-2,
        cycle=True)
    model = ResNetAMCADomClas(num_classes,
                              num_features,
                              num_filters=model_filters,
                              activation=activation_fn,
                              ca_decay=ca,
                              disc_hidden=disc_hidden,
                              num_domains=num_domains)
    optimizer = tf.keras.optimizers.Adam(learning_rate=learning_rate)

    summary_writer = tf.summary.create_file_writer(summary_writer_path)
    ckpt = tf.train.Checkpoint(model=model, optimizer=optimizer)
    ckpt_manager = tf.train.CheckpointManager(ckpt,
                                              checkpoint_path,
                                              max_to_keep=5)

    m_anneal = tf.Variable(0, dtype="float32")
    hp_lambda_anneal = tf.Variable(0, dtype="float32")
    for epoch in range(epochs):
        m_anneal.assign(tf.minimum(m * (epoch / (epochs / anneal)), m))
        hp_lambda_anneal.assign(tf.minimum(epoch / (epochs / anneal), 1.0))
        for datasets in zip(*train_datasets):
            train_step(*datasets, s, m_anneal, hp_lambda_anneal)

        pred_labels = []
        for data in time_test_set:
            pred_labels.extend(test_step(data[0]))
        temporal_test_acc(pred_labels, y_test_trg)
        if (epoch + 1) % log_images_freq == 0:
            cm = confusion_matrix(np.argmax(y_test_trg, axis=-1),
                                  np.argmax(pred_labels, axis=-1))
            cm_image = plot_to_image(
                plot_confusion_matrix(cm, class_names=classes))
            with summary_writer.as_default():
                tf.summary.image("Temporal Test Confusion Matrix",
                                 cm_image,
                                 step=epoch)

        pred_labels = []
        for data in src_test_set:
            pred_labels.extend(test_step(data[0]))
        source_test_acc(pred_labels, y_test_src)
        if (epoch + 1) % log_images_freq == 0:
            cm = confusion_matrix(np.argmax(y_test_src, axis=-1),
                                  np.argmax(pred_labels, axis=-1))
            cm_image = plot_to_image(
                plot_confusion_matrix(cm, class_names=classes))
            with summary_writer.as_default():
                tf.summary.image("Source Test Confusion Matrix",
                                 cm_image,
                                 step=epoch)

        pred_labels = []
        for data in office_test_set:
            pred_labels.extend(test_step(data[0]))
        office_test_acc(pred_labels, y_data_office)
        if (epoch + 1) % log_images_freq == 0:
            cm = confusion_matrix(np.argmax(y_data_office, axis=-1),
                                  np.argmax(pred_labels, axis=-1))
            cm_image = plot_to_image(
                plot_confusion_matrix(cm, class_names=classes))
            with summary_writer.as_default():
                tf.summary.image("Office Test Confusion Matrix",
                                 cm_image,
                                 step=epoch)

        pred_labels = []
        for data in server_test_set:
            pred_labels.extend(test_step(data[0]))
        server_test_acc(pred_labels, y_test_server)
        if (epoch + 1) % log_images_freq == 0:
            cm = confusion_matrix(np.argmax(y_test_server, axis=-1),
                                  np.argmax(pred_labels, axis=-1))
            cm_image = plot_to_image(
                plot_confusion_matrix(cm, class_names=classes))
            with summary_writer.as_default():
                tf.summary.image("Server Test Confusion Matrix",
                                 cm_image,
                                 step=epoch)

        pred_labels = []
        for data in conf_test_set:
            pred_labels.extend(test_step(data[0]))
        conference_test_acc(pred_labels, y_test_conf)
        if (epoch + 1) % log_images_freq == 0:
            cm = confusion_matrix(np.argmax(y_test_conf, axis=-1),
                                  np.argmax(pred_labels, axis=-1))
            cm_image = plot_to_image(
                plot_confusion_matrix(cm, class_names=classes))
            with summary_writer.as_default():
                tf.summary.image("Conference Test Confusion Matrix",
                                 cm_image,
                                 step=epoch)

        with summary_writer.as_default():
            tf.summary.scalar("temporal_test_acc",
                              temporal_test_acc.result(),
                              step=epoch)
            tf.summary.scalar("source_train_acc",
                              source_train_acc.result(),
                              step=epoch)
            tf.summary.scalar("source_test_acc",
                              source_test_acc.result(),
                              step=epoch)
            tf.summary.scalar("office_test_acc",
                              office_test_acc.result(),
                              step=epoch)
            tf.summary.scalar("server_test_acc",
                              server_test_acc.result(),
                              step=epoch)
            tf.summary.scalar("conference_test_acc",
                              conference_test_acc.result(),
                              step=epoch)
            tf.summary.scalar("cross_entropy_loss",
                              cross_entropy_loss.result(),
                              step=epoch)
            tf.summary.scalar("domain_loss", domain_loss.result(), step=epoch)

        if (epoch + 1) % save_freq == 0:
            ckpt_save_path = ckpt_manager.save()
            print('Saved checkpoint for epoch {} at {}'.format(
                epoch + 1, ckpt_save_path))

        temporal_test_acc.reset_states()
        source_train_acc.reset_states()
        source_test_acc.reset_states()
        office_test_acc.reset_states()
        server_test_acc.reset_states()
        conference_test_acc.reset_states()
        cross_entropy_loss.reset_states()
        domain_loss.reset_states()

    if save_freq != 0:
        ckpt_save_path = ckpt_manager.save()
        print('Saved final checkpoint at {}'.format(ckpt_save_path))
