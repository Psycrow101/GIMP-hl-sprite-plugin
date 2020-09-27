from gimpfu import *

from collections import namedtuple
from struct import pack, unpack
from math import sqrt
import os


class Sprite:
    MAGIC = 'IDSP'
    VERSION = 0x2

    HEADER_STRUCT = '<4s3If3IfI'
    FRAME_PARAMS_STRUCT = '<2i2I'

    TEXTURE_FORMAT_NORMAL, TEXTURE_FORMAT_ADDITIVE, TEXTURE_FORMAT_INDEXALPHA, TEXTURE_FORMAT_ALPHATEST = range(4)
    FRAME_TYPE_SINGLE, FRAME_TYPE_GROUP, FRAME_TYPE_ANGLED = range(3)

    SprHeader = namedtuple('SprHeader', [
        'magic',
        'version',
        'type',
        'format',
        'radius',
        'max_width',
        'max_height',
        'frames_number',
        'beam_length',
        'synch_type'
    ])

    FrameData = namedtuple('FrameData', [
        'type',
        'group_len',
        'intervals',
        'params',
        'indices'
    ])

    FrameParams = namedtuple('FrameParams', [
        'origin_x',
        'origin_y',
        'width',
        'height'
    ])

    @staticmethod
    def load_from_file(file_path):
        """
        Load Sprite from file.
        :param file_path: path to the sprite file
        :return: gimp image
        """

        fd = open(file_path, 'rb')
        header = Sprite._read_header(fd)
        palette = Sprite._read_palette(fd)
        frames = [Sprite._read_frame(fd) for _ in range(header.frames_number)]

        image = Sprite._make_image(header, palette, frames)
        # image.filename = os.path.basename(file_path)
        image.clean_all()
        return image

    @staticmethod
    def save_to_file(image, file_path, grouped_layers,
                     spr_type=0, texture_format=0):
        """
        Save Sprite to file.
        :param image: gimp image
        :param file_path: path to the output file
        :param grouped_layers: selected list of grouped layers with parasites
        :param spr_type: sprite type
        :param texture_format: sprite texture format
        """

        if os.path.exists(file_path):
            os.remove(file_path)

        fd = open(file_path, 'wb')

        frames_num = len(grouped_layers)
        max_width = image.width
        max_height = image.height
        radius = sqrt((max_width >> 1) * (max_width >> 1) + (max_height >> 1) * (max_height >> 1))

        header = Sprite.SprHeader(Sprite.MAGIC, Sprite.VERSION, spr_type, texture_format,
                                  radius, max_width, max_height, frames_num, 0, 1)

        gimp.progress_init('Preparing %d %s' % (frames_num, 'frame' if frames_num == 1 else 'frames'))
        frames = []
        for i, gl in enumerate(grouped_layers):
            gl_len = len(gl)
            if gl_len > 1:
                frame_type = gl[0].parasite_find('spr_type').flags
                group_len = gl_len - 1
                intervals, params, indices = [], [], []
                for sub_l in gl[1:]:
                    intervals.append(unpack('<f', sub_l.parasite_find('spr_interval').data[:4])[0])
                    params.append(Sprite._make_frame_params(sub_l))
                    indices.append(Sprite._make_frame_indices(sub_l))
            else:
                frame_type = Sprite.FRAME_TYPE_SINGLE
                group_len = 0
                intervals = None
                params = Sprite._make_frame_params(gl[0])
                indices = Sprite._make_frame_indices(gl[0])

            frames.append(Sprite.FrameData(frame_type, group_len, intervals, params, indices))
            gimp.progress_update(i / float(frames_num))

        gimp.progress_update(0.0)
        gimp.progress_init('Writing header')
        Sprite._write_header(fd, header)

        gimp.progress_init('Writing palette')
        Sprite._write_palette(fd, image.colormap)

        gimp.progress_init('Writing %d %s' % (frames_num, 'frame' if frames_num == 1 else 'frames'))
        for i, fr in enumerate(frames):
            Sprite._write_frame(fd, fr)
            gimp.progress_update(i / float(frames_num))

    @staticmethod
    def _read_header(fd):
        header = Sprite.SprHeader(*unpack(Sprite.HEADER_STRUCT, fd.read(40)))
        if header.magic != Sprite.MAGIC:
            raise ImportError('Invalid spr file')
        if header.version != Sprite.VERSION:
            raise ImportError('Invalid spr version: %d' % header.version)
        if header.frames_number < 1:
            raise ImportError('Invalid number of frames: %d' % header.frames_number)
        return header

    @staticmethod
    def _write_header(fd, header):
        fd.write(pack(Sprite.HEADER_STRUCT, *header[:]))

    @staticmethod
    def _read_palette(fd):
        pal_size = unpack('<H', fd.read(2))[0]
        return fd.read(pal_size * 3)

    @staticmethod
    def _write_palette(fd, palette):
        fd.write(pack('<H', len(palette) // 3))
        fd.write(palette)

    @staticmethod
    def _read_frame(fd):
        frame_type = unpack('<I', fd.read(4))[0]
        if frame_type == Sprite.FRAME_TYPE_SINGLE:
            group_len = 0
            intervals = None
            params = Sprite._read_frame_params(fd)
            indices = fd.read(params.width * params.height)
        else:
            group_len = unpack('<I', fd.read(4))[0]
            intervals = unpack('<%df' % group_len, fd.read(group_len * 4))
            params, indices = [], []
            for _ in range(group_len):
                p = Sprite._read_frame_params(fd)
                params.append(p)
                indices.append(fd.read(p.width * p.height))

        return Sprite.FrameData(frame_type, group_len, intervals, params, indices)

    @staticmethod
    def _write_frame(fd, frame):
        fd.write(pack('<I', frame.type))
        if frame.type == Sprite.FRAME_TYPE_SINGLE:
            Sprite._write_frame_params(fd, frame.params)
            fd.write(frame.indices)
        else:
            fd.write(pack('<I', frame.group_len))
            fd.write(pack('<%df' % len(frame.intervals), *frame.intervals))
            for i in range(frame.group_len):
                Sprite._write_frame_params(fd, frame.params[i])
                fd.write(frame.indices[i])

    @staticmethod
    def _read_frame_params(fd):
        return Sprite.FrameParams(*unpack(Sprite.FRAME_PARAMS_STRUCT, fd.read(16)))

    @staticmethod
    def _write_frame_params(fd, params):
        fd.write(pack(Sprite.FRAME_PARAMS_STRUCT, *params[:]))

    @staticmethod
    def _make_image(header, palette, frames):

        def make_pixel_data(indices):
            if header.format == Sprite.TEXTURE_FORMAT_INDEXALPHA:
                return ''.join(i + i for i in indices)

            if header.format == Sprite.TEXTURE_FORMAT_ALPHATEST:
                return ''.join(i + chr(0xff - (ord(i) // last_index * 0xff)) for i in indices)

            return indices

        def make_layer(layer_name, params, indices):
            layer = gimp.Layer(img, layer_name, params.width, params.height, layer_type, 100, NORMAL_MODE)
            rgn = layer.get_pixel_rgn(0, 0, layer.width, layer.height)
            rgn[:, :] = make_pixel_data(indices)
            layer.flush()
            return layer

        img = gimp.Image(header.max_width, header.max_height, INDEXED)
        img.colormap = palette

        img.attach_new_parasite('spr_type', header.type, '')
        img.attach_new_parasite('spr_format', header.format, '')

        last_index = len(palette) // 3 - 1

        layer_mode = ADDITION_MODE if header.format == Sprite.TEXTURE_FORMAT_ADDITIVE else NORMAL_MODE
        if header.format in (Sprite.TEXTURE_FORMAT_INDEXALPHA, Sprite.TEXTURE_FORMAT_ALPHATEST):
            layer_type = INDEXEDA_IMAGE
        else:
            layer_type = INDEXED_IMAGE

        for i, fr in enumerate(frames):
            if fr.type == Sprite.FRAME_TYPE_SINGLE:
                layer_name = 'Frame %d' % i
                layer = make_layer(layer_name, fr.params, fr.indices)
                layer.attach_new_parasite('spr_origins', 0, pack('<2i', fr.params.origin_x, fr.params.origin_y))
                pdb.gimp_image_insert_layer(img, layer, None, 0)
            else:
                layer = gimp.GroupLayer(img)
                layer.name = 'Group %d' % i
                pdb.gimp_image_insert_layer(img, layer, None, 0)

                for j in range(fr.group_len):
                    sub_layer_name = 'Frame %d.%d' % (i, j)
                    params = fr.params[j]
                    sub_layer = make_layer(sub_layer_name, params, fr.indices[j])
                    sub_layer.attach_new_parasite('spr_interval', 0, pack('<f', fr.intervals[j]))
                    sub_layer.attach_new_parasite('spr_origins', 0, pack('<2i', params.origin_x, params.origin_y))
                    img.insert_layer(sub_layer, layer)

            layer.mode = layer_mode

            layer.attach_new_parasite('spr_type', fr.type, '')

        return img

    @staticmethod
    def _make_frame_params(layer):
        origin_x, origin_y = unpack('<2i', layer.parasite_find('spr_origins').data[:8])
        return Sprite.FrameParams(origin_x, origin_y, layer.width, layer.height)

    @staticmethod
    def _make_frame_indices(layer):
        indices = layer.get_pixel_rgn(0, 0, layer.width, layer.height)[:, :]
        if layer.type == INDEXEDA_IMAGE:
            indices = indices[::2]
        return indices
