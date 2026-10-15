from collections import namedtuple
from functools import lru_cache
from math import isfinite, sqrt
from struct import calcsize, pack, unpack
import os
import tempfile


class Sprite:
    MAGIC = b'IDSP'
    VERSION_QUAKE, VERSION_GS, VERSION_DDS = 0x1, 0x2, 0x3
    VERSIONS = (VERSION_QUAKE, VERSION_GS, VERSION_DDS)

    HEADER_PREFIX_STRUCT = '<4s2I'  # magic, version, type
    HEADER_TAIL_STRUCT = '<f3IfI'   # radius, max_width, max_height, frames_number, beam_length, synch_type
    HEADER_STRUCT = '<4s3If3IfI'

    FRAME_PARAMS_STRUCT = '<2i2I'
    (
        TEXTURE_FORMAT_NORMAL,
        TEXTURE_FORMAT_ADDITIVE,
        TEXTURE_FORMAT_INDEXALPHA,
        TEXTURE_FORMAT_ALPHATEST,
    ) = range(4)
    FRAME_TYPE_SINGLE, FRAME_TYPE_GROUP, FRAME_TYPE_ANGLED = range(3)

    SprHeader = namedtuple(
        'SprHeader',
        [
            'magic',
            'version',
            'type',
            'format',
            'radius',
            'max_width',
            'max_height',
            'frames_number',
            'beam_length',
            'synch_type',
        ],
    )
    FrameData = namedtuple(
        'FrameData',
        [
            'type',
            'group_len',
            'intervals',
            'params',
            'indices',
        ],
    )
    FrameParams = namedtuple(
        'FrameParams',
        [
            'origin_x',
            'origin_y',
            'width',
            'height',
        ],
    )

    @staticmethod
    def load_from_file(file_path, last_dds_frame=False):
        """
        Load Sprite from file.

        :param file_path: path to the sprite file
        :param last_dds_frame: load only the last DDS frame if available
        :return: GIMP images
        """

        with open(file_path, 'rb') as fd:
            header = Sprite._read_header(fd)

            if header.version == Sprite.VERSION_DDS:
                dds_frames = Sprite._read_dds_frames(fd, header.frames_number)
                images = Sprite._load_dds_frames(dds_frames[-1:] if last_dds_frame else dds_frames)
            else:
                palette = Sprite._read_palette(fd)
                frames = [Sprite._read_frame(fd) for _ in range(header.frames_number)]
                images = [Sprite._make_image(header, palette, frames)]

        for image in images:
            attach_data(image, 'spr_type', pack('<I', header.type))
            attach_data(image, 'spr_format', pack('<I', header.format))
            image.clean_all()

        return images

    @staticmethod
    def save_to_file(image, file_path, grouped_layers, spr_type=0, texture_format=0):
        """
        Save Sprite to file.

        :param image: indexed GIMP image
        :param file_path: path to the output file
        :param grouped_layers: selected list of grouped layers with parasites
        :param spr_type: sprite type
        :param texture_format: sprite texture format
        """
        Gimp, _, _, _ = gimp_api()

        if not grouped_layers:
            raise ValueError('Select at least one frame to export')
        if image.get_base_type() != Gimp.ImageBaseType.INDEXED:
            raise ValueError('Sprite export requires an indexed image')

        frames_num = len(grouped_layers)
        max_width, max_height = image.get_width(), image.get_height()
        radius = sqrt((max_width >> 1) ** 2 + (max_height >> 1) ** 2)

        header = Sprite.SprHeader(
            Sprite.MAGIC,
            Sprite.VERSION_GS,
            spr_type,
            texture_format,
            radius,
            max_width,
            max_height,
            frames_num,
            0, 1,
        )

        frames = []
        Gimp.progress_init('Preparing sprite frames')

        for i, layers in enumerate(grouped_layers):
            # TODO: test
            if len(layers) > 1:
                frame_type = metadata_int(layers[0], 'spr_type', Sprite.FRAME_TYPE_GROUP)

                if frame_type not in (
                    Sprite.FRAME_TYPE_GROUP,
                    Sprite.FRAME_TYPE_ANGLED,
                ):
                    frame_type = Sprite.FRAME_TYPE_GROUP

                if frame_type == Sprite.FRAME_TYPE_ANGLED:
                    directions = list(reversed(layers[0].get_children()))
                    if len(layers) - 1 < 8 or layers[1:] != directions:
                        raise ValueError(
                            'Keep all angled frame directions in their layer order (at least eight frames)'
                        )

                intervals = []
                elapsed = 0.0

                for j, layer in enumerate(layers[1:]):
                    interval = layer.get_parasite('spr_interval')
                    if frame_type == Sprite.FRAME_TYPE_GROUP:
                        duration = layer.get_parasite('spr_duration')
                        duration = unpack('<f', parasite_bytes(duration)[:4])[0] if duration else 0.1
                        if not isfinite(duration) or duration <= 0:
                            raise ValueError('Animation frame duration must be positive')

                        elapsed += duration
                        intervals.append(elapsed)
                    else:
                        intervals.append(unpack('<f', parasite_bytes(interval)[:4])[0] if interval else (j + 1) * 0.1)

                frames.append(
                    Sprite.FrameData(
                        frame_type,
                        len(layers) - 1,
                        intervals,
                        [Sprite._make_frame_params(layer) for layer in layers[1:]],
                        [Sprite._make_frame_indices(layer) for layer in layers[1:]],
                    )
                )
            else:
                frames.append(
                    Sprite.FrameData(
                        Sprite.FRAME_TYPE_SINGLE,
                        0,
                        None,
                        Sprite._make_frame_params(layers[0]),
                        Sprite._make_frame_indices(layers[0]),
                    )
                )

            Gimp.progress_update((i + 1) / len(grouped_layers))

        # Replace the destination only after the complete sprite has been written.
        fd_number, temp_path = tempfile.mkstemp(prefix='.spr-', dir=os.path.dirname(os.path.abspath(file_path)))

        try:
            with os.fdopen(fd_number, 'wb') as fd:
                Sprite._write_header(fd, header)

                Sprite._write_palette(fd, palette_bytes(image))

                for frame in frames:
                    Sprite._write_frame(fd, frame)

            os.replace(temp_path, file_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

            Gimp.progress_end()

    @staticmethod
    def _read_header(fd):
        magic, version, spr_type = unpack(
            Sprite.HEADER_PREFIX_STRUCT,
            Sprite._read_exact(fd, calcsize(Sprite.HEADER_PREFIX_STRUCT)),
        )

        if version == Sprite.VERSION_QUAKE:
            spr_format = Sprite.TEXTURE_FORMAT_ALPHATEST
        else:
            (spr_format,) = unpack('<I', Sprite._read_exact(fd, 4))

        radius, max_width, max_height, frames_number, beam_length, synch_type = unpack(
            Sprite.HEADER_TAIL_STRUCT,
            Sprite._read_exact(fd, calcsize(Sprite.HEADER_TAIL_STRUCT)),
        )

        header = Sprite.SprHeader(
            magic, version, spr_type, spr_format,
            radius, max_width, max_height, frames_number, beam_length, synch_type,
        )

        if header.magic != Sprite.MAGIC:
            raise ValueError('Invalid SPR signature')
        if header.version not in Sprite.VERSIONS:
            raise ValueError('Unsupported SPR version: %d' % header.version)
        if header.frames_number < 1 or header.max_width < 1 or header.max_height < 1:
            raise ValueError('Invalid sprite dimensions or number of frames')
        if header.type > 4 or header.format > 3:
            raise ValueError('Invalid sprite type or texture format')

        return header

    @staticmethod
    def _write_header(fd, header):
        fd.write(pack(Sprite.HEADER_STRUCT, *header))

    @staticmethod
    def _read_palette(fd):
        size = unpack('<H', Sprite._read_exact(fd, 2))[0]
        if not 1 <= size <= 256:
            raise ValueError('Sprite palette must contain 1 to 256 colors')

        return Sprite._read_exact(fd, size * 3)

    @staticmethod
    def _write_palette(fd, palette):
        fd.write(pack('<H', len(palette) // 3))
        fd.write(palette)

    @staticmethod
    def _read_frame(fd):
        frame_type = unpack('<I', Sprite._read_exact(fd, 4))[0]
        if frame_type == Sprite.FRAME_TYPE_SINGLE:
            params = Sprite._read_frame_params(fd)
            indices = Sprite._read_exact(fd, params.width * params.height)

            return Sprite.FrameData(
                frame_type,
                0,
                None,
                params,
                indices,
            )

        if frame_type not in (Sprite.FRAME_TYPE_GROUP, Sprite.FRAME_TYPE_ANGLED):
            raise ValueError('Invalid sprite frame type: %d' % frame_type)

        group_len = unpack('<I', Sprite._read_exact(fd, 4))[0]
        if group_len < 1:
            raise ValueError('Empty sprite frame group')

        intervals = unpack('<%df' % group_len, Sprite._read_exact(fd, group_len * 4))
        params, indices = [], []

        for _ in range(group_len):
            p = Sprite._read_frame_params(fd)
            params.append(p)
            indices.append(Sprite._read_exact(fd, p.width * p.height))

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
            for params, indices in zip(frame.params, frame.indices):
                Sprite._write_frame_params(fd, params)
                fd.write(indices)

    @staticmethod
    def _read_frame_params(fd):
        params = Sprite.FrameParams(*unpack(Sprite.FRAME_PARAMS_STRUCT, Sprite._read_exact(fd, 16)))
        if params.width < 1 or params.height < 1:
            raise ValueError('Invalid sprite frame dimensions')

        return params

    @staticmethod
    def _write_frame_params(fd, params):
        fd.write(pack(Sprite.FRAME_PARAMS_STRUCT, *params))

    @staticmethod
    def _make_image(header, palette, frames):
        Gimp, Gegl, Babl, _ = gimp_api()
        from gi.repository import GLib

        image = Gimp.Image.new(header.max_width, header.max_height, Gimp.ImageBaseType.INDEXED)

        try:
            colormap = image.get_palette()
            color_format = Babl.format("R'G'B' u8")
            for i in range(0, len(palette), 3):
                color = Gegl.Color.new('black')
                color.set_bytes(color_format, GLib.Bytes.new(palette[i : i + 3]))
                colormap.add_entry(None, color)

            last_index = len(palette) // 3 - 1
            with_alpha = header.format in (
                Sprite.TEXTURE_FORMAT_INDEXALPHA,
                Sprite.TEXTURE_FORMAT_ALPHATEST,
            )
            layer_type = Gimp.ImageType.INDEXEDA_IMAGE if with_alpha else Gimp.ImageType.INDEXED_IMAGE
            mode = (
                Gimp.LayerMode.ADDITION_LEGACY
                if header.format == Sprite.TEXTURE_FORMAT_ADDITIVE
                else Gimp.LayerMode.NORMAL_LEGACY
            )

            def make_layer(name, params, indices, parent=None):
                if any(index > last_index for index in indices):
                    raise ValueError('Sprite pixel references a missing palette color')

                layer = Gimp.Layer.new(image, name, params.width, params.height, layer_type, 100, mode)
                image.insert_layer(layer, parent, 0)

                pixels = indices
                if with_alpha:
                    pixels = bytearray(len(indices) * 2)
                    pixels[::2] = indices
                    pixels[1::2] = (
                        indices
                        if header.format == Sprite.TEXTURE_FORMAT_INDEXALPHA
                        else bytes(0 if index == last_index else 255 for index in indices)
                    )

                buffer = layer.get_buffer()
                buffer.set(
                    Gegl.Rectangle.new(0, 0, params.width, params.height),
                    Babl.get_name(layer.get_format()),
                    bytes(pixels),
                )
                buffer.flush()
                layer.update(0, 0, params.width, params.height)
                attach_data(layer, 'spr_origins', pack('<2i', params.origin_x, params.origin_y))
                return layer

            for i, frame in enumerate(frames):
                if frame.type == Sprite.FRAME_TYPE_SINGLE:
                    layer = make_layer('Frame %d' % i, frame.params, frame.indices)
                else:
                    layer = Gimp.GroupLayer.new(image, 'Group %d' % i)
                    image.insert_layer(layer, None, 0)
                    layer.set_mode(mode)

                    for j in range(frame.group_len):
                        child = make_layer(
                            'Frame %d.%d' % (i, j),
                            frame.params[j],
                            frame.indices[j],
                            layer,
                        )
                        attach_data(child, 'spr_interval', pack('<f', frame.intervals[j]))
                        if frame.type == Sprite.FRAME_TYPE_GROUP:
                            previous = frame.intervals[j - 1] if j else 0.0
                            attach_data(
                                child,
                                'spr_duration',
                                pack('<f', frame.intervals[j] - previous),
                            )

                attach_data(layer, 'spr_type', pack('<I', frame.type))

            return image
        except Exception:
            image.delete()
            raise

    @staticmethod
    def _make_frame_params(layer):
        origins = layer.get_parasite('spr_origins')
        width, height = layer.get_width(), layer.get_height()

        x, y = unpack('<2i', parasite_bytes(origins)[:8]) if origins else (-(width // 2), height // 2)

        return Sprite.FrameParams(x, y, width, height)

    @staticmethod
    def _make_frame_indices(layer):
        pixels = layer_pixels(layer)
        if layer.has_alpha():
            return bytearray(pixels[::2])

        return pixels

    @staticmethod
    def _read_dds_frames(fd, num):
        data = fd.read()

        def valid_header(position):
            return (
                position >= 0
                and len(data) - position >= 128
                and data[position : position + 4] == b'DDS '
                and unpack('<I', data[position + 4 : position + 8])[0] == 124
                and unpack('<I', data[position + 76 : position + 80])[0] == 32
                and all(unpack('<2I', data[position + 12 : position + 20]))
            )

        position, frames = 0, []

        for i in range(num):
            if not valid_header(position):
                raise ValueError('Invalid DDS frame in sprite')

            end = data.find(b'DDS ', position + 128) if i < num - 1 else -1
            while end != -1 and not valid_header(end):
                end = data.find(b'DDS ', end + 4)
            if end == -1:
                if i < num - 1:
                    raise ValueError('Missing DDS frame in sprite')
                end = len(data)

            frames.append(data[position:end])
            position = end

        return frames

    @staticmethod
    def _load_dds_frames(frames):
        Gimp, _, _, Gio = gimp_api()
        images = []

        try:
            for data in frames:
                fd_number, path = tempfile.mkstemp(suffix='.dds')

                try:
                    with os.fdopen(fd_number, 'wb') as fd:
                        fd.write(data)

                    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))
                    if image is None:
                        raise ValueError('Unable to load DDS sprite frame')
                    images.append(image)
                finally:
                    os.unlink(path)

            return images
        except Exception:
            for image in images:
                image.delete()
            raise

    @staticmethod
    def _read_exact(fd, size):
        data = fd.read(size)
        if len(data) != size:
            raise ValueError('Truncated sprite file')

        return data


@lru_cache(maxsize=1)
def gimp_api():
    import gi

    gi.require_version('Gimp', '3.0')
    gi.require_version('Gegl', '0.4')
    gi.require_version('Babl', '0.1')
    from gi.repository import Gimp, Gegl, Babl, Gio

    Gegl.init(None)
    return Gimp, Gegl, Babl, Gio


def attach_data(item, name, data):
    Gimp, _, _, _ = gimp_api()
    # The introspection binding exposes parasite data as signed char values.
    item.attach_parasite(Gimp.Parasite.new(name, 1, [value if value < 128 else value - 256 for value in data]))


def metadata_int(item, name, default=0):
    parasite = item.get_parasite(name)
    if not parasite:
        return default
    data = parasite_bytes(parasite)
    return unpack('<I', data[:4])[0] if len(data) >= 4 else parasite.get_flags()


def parasite_bytes(parasite):
    return bytes(value & 255 for value in parasite.get_data())


def palette_bytes(image):
    _, _, Babl, _ = gimp_api()
    data, _ = image.get_palette().get_colormap(Babl.format("R'G'B' u8"))
    return bytes(data)


def layer_pixels(layer):
    """Read native indexed bytes, preserving duplicate palette entries."""
    _, Gegl, _, _ = gimp_api()
    rectangle = Gegl.Rectangle.new(0, 0, layer.get_width(), layer.get_height())
    return bytes(layer.get_buffer().get(rectangle, 1.0, None, Gegl.AbyssPolicy.NONE))
