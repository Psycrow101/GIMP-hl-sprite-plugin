#!/usr/bin/env python3
"""GIMP 3 plug-in for Half-Life sprites (.spr)."""

import os
import json
import sys
from struct import pack

import gi

gi.require_version('Gimp', '3.0')
gi.require_version('GimpUi', '3.0')
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gimp, GimpUi, Gtk, GdkPixbuf, GObject, GLib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spr import Sprite, attach_data, layer_pixels, metadata_int, palette_bytes

AUTHOR = 'Psycrow'
EDITOR_PROC = 'hl-spr-export-dialog'
LOAD_PROC = 'file-hl-spr-load'
LOAD_THUMB_PROC = 'file-hl-spr-load-thumb'
SAVE_PROC = 'file-hl-spr-save'


def load_spr_thumbnail(procedure, file, thumb_size, config, data):
    image = None

    try:
        image = Sprite.load_from_file(local_path(file), True)[0]
        width, height = image.get_width(), image.get_height()

        scale = min(1.0, max(1, thumb_size) / max(width, height))
        if scale < 1.0:
            image.scale(max(1, int(width * scale)), max(1, int(height * scale)))

        return Gimp.ValueArray.new_from_values(
            [
                GObject.Value(Gimp.PDBStatusType, Gimp.PDBStatusType.SUCCESS),
                GObject.Value(Gimp.Image, image),
                GObject.Value(GObject.TYPE_INT, width),
                GObject.Value(GObject.TYPE_INT, height),
                GObject.Value(Gimp.ImageType, image.get_layers()[0].type()),
                GObject.Value(GObject.TYPE_INT, len(image.get_layers())),
            ]
        )
    except Exception as error:
        if image:
            image.delete()

        return error_values(procedure, error)


def load_spr(procedure, run_mode, file, metadata, flags, config, data):
    try:
        images = Sprite.load_from_file(local_path(file))

        for image in images[:-1]:
            if run_mode == Gimp.RunMode.INTERACTIVE:
                Gimp.Display.new(image)
            else:
                image.delete()

        values = Gimp.ValueArray.new_from_values(
            [
                GObject.Value(Gimp.PDBStatusType, Gimp.PDBStatusType.SUCCESS),
                GObject.Value(Gimp.Image, images[-1]),
            ]
        )
        return values, flags
    except Exception as error:
        return error_values(procedure, error), flags


def save_spr(procedure, run_mode, image, file, options, metadata, config, data):
    source_snapshot, spr_image, dialog = None, None, None

    try:
        sprite_type = config.get_property('sprite-type')
        texture_format = config.get_property('texture-format')

        if run_mode == Gimp.RunMode.INTERACTIVE:
            settings = json.loads(config.get_property('frame-settings') or '{}')
            if settings.get('image') != image.get_id():
                if image.get_parasite('spr_type'):
                    sprite_type = metadata_int(image, 'spr_type')
                if image.get_parasite('spr_format'):
                    texture_format = metadata_int(image, 'spr_format')

        if sprite_type < 0:
            sprite_type = metadata_int(image, 'spr_type')
        if texture_format < 0:
            texture_format = metadata_int(image, 'spr_format')

        # TODO: Why INDEXED check?
        if run_mode == Gimp.RunMode.INTERACTIVE and image.get_base_type() != Gimp.ImageBaseType.INDEXED:
            # Every preview conversion starts with the same unmodified pixels.
            source_snapshot = image.duplicate()

        spr_image = (source_snapshot or image).duplicate()
        prepare_export(spr_image, texture_format)

        frame_settings = None
        if run_mode == Gimp.RunMode.INTERACTIVE:
            GimpUi.init('file-spr')
            dialog = ExportDialog(spr_image, sprite_type, texture_format, source_image=source_snapshot)

            if dialog.run() != Gtk.ResponseType.OK:
                return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

            spr_image = dialog.image
            groups = dialog.selected_groups()
            sprite_type, texture_format = (
                dialog.cb_st.get_active(),
                dialog.cb_tf.get_active(),
            )
            frame_settings = remember_frames(dialog, image)
        elif run_mode == Gimp.RunMode.WITH_LAST_VALS:
            groups = group_frames(restore_frames(image, spr_image, config.get_property('frame-settings')))
        else:
            groups = group_frames(frame_layers(spr_image))

        Sprite.save_to_file(spr_image, local_path(file), groups, sprite_type, texture_format)

        config.set_property('sprite-type', sprite_type)
        config.set_property('texture-format', texture_format)
        if frame_settings is not None:
            config.set_property('frame-settings', frame_settings)

        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)
    except Exception as error:
        return error_values(procedure, error)
    finally:
        if dialog:
            spr_image = dialog.image
            dialog.destroy()

        if spr_image:
            spr_image.delete()

        if source_snapshot:
            source_snapshot.delete()


def local_path(file):
    path = file.get_path()

    if path is None:
        raise ValueError('SPR files must be stored on the local filesystem')

    return path


def error_values(procedure, error):
    return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(error)))


def frame_layers(image):
    """List animation frames from the bottom of the layer stack upward."""

    def leaves(layer):
        if layer.is_group():
            for child in reversed(layer.get_children()):
                yield from leaves(child)
        else:
            yield layer

    return [frame for layer in reversed(image.get_layers()) for frame in leaves(layer)]


def group_frames(layers):
    grouped, added = [], set()

    for layer in layers:
        if layer.get_id() in added:
            continue

        parent = layer.get_parent()
        if parent:
            children = [child for child in layers if child.get_parent() == parent]
            grouped.append([parent] + children)
            added.update(child.get_id() for child in children)
        else:
            grouped.append([layer])
            added.add(layer.get_id())

    return grouped


def remember_frames(dialog, source_image):
    identifiers = {
        copy.get_id(): source.get_id() for source, copy in zip(frame_layers(source_image), frame_layers(dialog.image))
    }

    return json.dumps(
        {
            'image': source_image.get_id(),
            'frames': [
                [
                    identifiers[row[dialog.LS_LAYER].get_id()],
                    row[dialog.LS_EXPORT],
                    row[dialog.LS_ORIGIN_X],
                    row[dialog.LS_ORIGIN_Y],
                ]
                for row in dialog.liststore
            ],
        }
    )


def restore_frames(source_image, copy_image, serialized):
    layers = frame_layers(copy_image)
    if not serialized:
        return layers

    settings = json.loads(serialized)
    if settings['image'] != source_image.get_id():
        return layers

    by_id = {source.get_id(): copy for source, copy in zip(frame_layers(source_image), layers)}
    selected, seen = [], set()

    for identifier, include, x, y in settings['frames']:
        layer = by_id.get(identifier)
        if layer is not None:
            seen.add(identifier)
            if include:
                attach_data(layer, 'spr_origins', pack('<2i', x, y))
                selected.append(layer)

    selected.extend(layer for identifier, layer in by_id.items() if identifier not in seen)
    return selected


def prepare_export(image, texture_format):
    if image.get_base_type() == Gimp.ImageBaseType.INDEXED:
        return

    if image.get_base_type() == Gimp.ImageBaseType.GRAY:
        if not image.convert_rgb():
            raise ValueError('Unable to convert the sprite to RGB colors')

    if image.get_precision() != Gimp.Precision.U8_NON_LINEAR:
        if not image.convert_precision(Gimp.Precision.U8_NON_LINEAR):
            raise ValueError('Unable to convert the sprite to 8-bit nonlinear precision')

    def remove_groups(layers):
        tree = []
        for layer in layers:
            if layer.is_group():
                properties = (layer.get_name(), layer.get_visible(), layer.get_opacity(), layer.get_mode())
                parasites = [layer.get_parasite(name) for name in layer.get_parasite_list()]
                children = remove_groups(layer.get_children())
                tree.append((properties, parasites, children))
                if not image.remove_layer(layer):
                    raise ValueError('Unable to prepare sprite frame groups')
            else:
                tree.append(layer)
                if not image.reorder_item(layer, None, 0):
                    raise ValueError('Unable to prepare sprite frames')
        return tree

    def restore_groups(tree, parent=None):
        for position, node in enumerate(tree):
            if isinstance(node, tuple):
                (name, visible, opacity, mode), parasites, children = node
                group = Gimp.GroupLayer.new(image, name)
                if not image.insert_layer(group, parent, position):
                    raise ValueError('Unable to restore sprite frame groups')
                group.set_visible(visible)
                group.set_opacity(opacity)
                group.set_mode(mode)
                for parasite in parasites:
                    group.attach_parasite(parasite)
                restore_groups(children, group)
            elif not image.reorder_item(node, parent, position):
                raise ValueError('Unable to restore sprite frames')

    # Indexed conversion requires a flat stack; each leaf remains a separate frame.
    tree = remove_groups(image.get_layers())

    colors = 255 if texture_format == Sprite.TEXTURE_FORMAT_ALPHATEST else 256
    if not image.convert_indexed(
        Gimp.ConvertDitherType.NONE,
        Gimp.ConvertPaletteType.GENERATE,
        colors,
        False,
        False,
        '',
    ):
        raise ValueError('Unable to convert the sprite to indexed colors')

    restore_groups(tree)

    if texture_format == Sprite.TEXTURE_FORMAT_ALPHATEST:
        from spr import gimp_api

        _, Gegl, _, _ = gimp_api()
        palette = image.get_palette()
        while palette.get_color_count() < 256:
            palette.add_entry('Transparent', Gegl.Color.new('blue'))


class ExportDialog(GimpUi.Dialog):
    THUMB_MAXSIZE = 128
    MIN_ORIGIN, MAX_ORIGIN = -8192, 8192

    (
        LS_LAYER,
        LS_PIXBUF,
        LS_SIZE_INFO,
        LS_EXPORT,
        LS_ORIGIN_X,
        LS_ORIGIN_Y,
        LS_THUMBDATA,
    ) = range(7)

    def __init__(self, image, sprite_type, texture_format, source_image=None):
        super().__init__(title='Export Image as Half-Life Sprite', role=EDITOR_PROC)

        self.image = image
        self.source_image = source_image
        self.prepared_format = texture_format
        self.palette = palette_bytes(image)

        self.add_button('_Cancel', Gtk.ResponseType.CANCEL)
        self.add_button('_Export', Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20, margin=12)
        box.pack_start(self.make_export_options(sprite_type, texture_format), False, False, 0)
        self.frames_frame = self.make_frames_view()
        box.pack_start(self.frames_frame, True, True, 0)

        self.get_content_area().pack_start(box, True, True, 0)
        self.get_content_area().show_all()

    def update_thumbnails(self, widget=None):
        if hasattr(self, 'liststore'):
            texture_format = self.cb_tf.get_active()

            if self.source_image is not None and texture_format != self.prepared_format:
                preview = self.source_image.duplicate()
                try:
                    prepare_export(preview, texture_format)
                    replacements = {
                        old.get_id(): new for old, new in zip(frame_layers(self.image), frame_layers(preview))
                    }
                    thumbnails = {identifier: self.thumbnail_data(layer) for identifier, layer in replacements.items()}
                    palette = palette_bytes(preview)
                except Exception:
                    preview.delete()
                    raise

                previous = self.image
                self.image = preview
                self.palette = palette
                self.prepared_format = texture_format

                for row in self.liststore:
                    identifier = row[self.LS_LAYER].get_id()
                    row[self.LS_LAYER] = replacements[identifier]
                    row[self.LS_THUMBDATA] = thumbnails[identifier]

                previous.delete()

            for row in self.liststore:
                row[self.LS_PIXBUF] = self.thumbnail(row[self.LS_THUMBDATA], texture_format)

    def make_export_options(self, sprite_type, texture_format):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self.cb_st = self.combo(
            [
                'VP Parallel Upright',
                'Facing Upright',
                'VP Parallel',
                'Oriented',
                'VP Parallel Oriented',
            ],
            sprite_type,
        )
        box.pack_start(self.option_frame('Sprite Type:', self.cb_st), False, False, 0)

        self.cb_tf = self.combo(['Normal', 'Additive', 'Indexalpha', 'Alphatest'], texture_format)
        self.cb_tf.connect('changed', self.update_thumbnails)
        box.pack_start(self.option_frame('Texture Format:', self.cb_tf), False, False, 0)

        origins = Gtk.Grid(column_spacing=8, row_spacing=8)
        self.sb_oo_x, self.sb_oo_y = self.spin(), self.spin()

        self.sb_oo_x.set_value(0)
        self.sb_oo_y.set_value(0)

        origins.attach(Gtk.Label(label='Origin X:'), 0, 0, 1, 1)
        origins.attach(self.sb_oo_x, 1, 0, 1, 1)

        origins.attach(Gtk.Label(label='Origin Y:'), 0, 1, 1, 1)
        origins.attach(self.sb_oo_y, 1, 1, 1, 1)

        button = Gtk.Button(label='Add offsets')
        button.connect('clicked', self.add_offsets)
        origins.attach(button, 0, 2, 2, 1)

        box.pack_start(self.option_frame('Add Origin Offsets:', origins), False, False, 0)
        return box

    def make_frames_view(self):
        self.liststore = Gtk.ListStore(object, GdkPixbuf.Pixbuf, str, bool, int, int, object)

        for layer in frame_layers(self.image):
            width, height = layer.get_width(), layer.get_height()
            thumbnail_data = self.thumbnail_data(layer)
            params = Sprite._make_frame_params(layer)

            self.liststore.append(
                [
                    layer,
                    self.thumbnail(thumbnail_data, self.cb_tf.get_active()),
                    'Size: %d x %d' % (width, height),
                    True,
                    params.origin_x,
                    params.origin_y,
                    thumbnail_data,
                ]
            )

        tree = Gtk.TreeView(model=self.liststore)
        tree.set_reorderable(True)
        tree.set_enable_search(False)
        tree.set_grid_lines(Gtk.TreeViewGridLines.BOTH)

        renderer = Gtk.CellRendererToggle()
        renderer.connect('toggled', self.toggle_export)
        tree.append_column(Gtk.TreeViewColumn('Export', renderer, active=self.LS_EXPORT))

        renderer = Gtk.CellRendererPixbuf()
        tree.append_column(Gtk.TreeViewColumn('Frame', renderer, pixbuf=self.LS_PIXBUF))

        renderer = Gtk.CellRendererText()
        tree.append_column(Gtk.TreeViewColumn('Size', renderer, text=self.LS_SIZE_INFO))

        for title, column in (
            ('Origin X', self.LS_ORIGIN_X),
            ('Origin Y', self.LS_ORIGIN_Y),
        ):
            adjustment = Gtk.Adjustment(lower=self.MIN_ORIGIN, upper=self.MAX_ORIGIN, step_increment=1)
            renderer = Gtk.CellRendererSpin(editable=True, adjustment=adjustment, digits=0)
            renderer.connect('edited', self.edit_origin, column)
            tree.append_column(Gtk.TreeViewColumn(title, renderer, text=column))

        # Empty column
        tree.append_column(Gtk.TreeViewColumn())

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(640, self.THUMB_MAXSIZE * 4)
        scroll.add(tree)

        return self.option_frame('Frames to export: %d' % len(self.liststore), scroll)

    def selected_groups(self):
        layers = []

        for row in self.liststore:
            if row[self.LS_EXPORT]:
                layer = row[self.LS_LAYER]
                attach_data(
                    layer,
                    'spr_origins',
                    pack('<2i', row[self.LS_ORIGIN_X], row[self.LS_ORIGIN_Y]),
                )
                layers.append(layer)

        return group_frames(layers)

    def combo(self, labels, active):
        widget = Gtk.ComboBoxText()

        for label in labels:
            widget.append_text(label)

        widget.set_active(active)
        return widget

    def spin(self):
        return Gtk.SpinButton.new_with_range(self.MIN_ORIGIN, self.MAX_ORIGIN, 1)

    @staticmethod
    def option_frame(title, child):
        frame = GimpUi.Frame(label=title)
        frame.add(child)
        return frame

    def thumbnail(self, thumbnail_data, texture_format):
        indices, width, height, alpha = thumbnail_data
        last_index = len(self.palette) // 3 - 1
        pixels = bytearray(width * height * 4)

        for i, index in enumerate(indices):
            color = self.palette[index * 3 : index * 3 + 3]
            pixels[i * 4 : i * 4 + 3] = color

            if texture_format == Sprite.TEXTURE_FORMAT_INDEXALPHA:
                opacity = index
            elif texture_format == Sprite.TEXTURE_FORMAT_ADDITIVE:
                opacity = sum(color) // 3
            elif texture_format == Sprite.TEXTURE_FORMAT_ALPHATEST:
                opacity = 0 if index == last_index or (alpha and alpha[i] < 128) else 255
            else:
                opacity = 255

            pixels[i * 4 + 3] = opacity

        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(bytes(pixels)),
            GdkPixbuf.Colorspace.RGB,
            True,
            8,
            width,
            height,
            width * 4,
        )
        pixbuf = pixbuf.copy()

        scale = min(1.0, self.THUMB_MAXSIZE / max(width, height))
        if scale < 1:
            pixbuf = pixbuf.scale_simple(
                max(1, int(width * scale)),
                max(1, int(height * scale)),
                GdkPixbuf.InterpType.NEAREST,
            )

        return pixbuf

    @staticmethod
    def thumbnail_data(layer):
        pixels = layer_pixels(layer)
        indices, alpha = (pixels[::2], pixels[1::2]) if layer.has_alpha() else (pixels, None)
        return indices, layer.get_width(), layer.get_height(), alpha

    def add_offsets(self, button):
        for row in self.liststore:
            for column, spin in (
                (self.LS_ORIGIN_X, self.sb_oo_x),
                (self.LS_ORIGIN_Y, self.sb_oo_y),
            ):
                row[column] = min(
                    self.MAX_ORIGIN,
                    max(self.MIN_ORIGIN, row[column] + spin.get_value_as_int()),
                )

    def toggle_export(self, renderer, path):
        self.liststore[path][self.LS_EXPORT] = not self.liststore[path][self.LS_EXPORT]

        count = sum(row[self.LS_EXPORT] for row in self.liststore)
        self.get_widget_for_response(Gtk.ResponseType.OK).set_sensitive(count > 0)
        self.frames_frame.set_label('Frames to export: %d' % count)

    def edit_origin(self, renderer, path, value, column):
        try:
            value = int(value)
        except ValueError:
            return

        self.liststore[path][column] = min(self.MAX_ORIGIN, max(self.MIN_ORIGIN, value))


class HalfLifeSpritePlugin(Gimp.PlugIn):
    def do_set_i18n(self, name):
        return False

    def do_query_procedures(self):
        return [LOAD_THUMB_PROC, LOAD_PROC, SAVE_PROC]

    def do_create_procedure(self, name):
        if name == LOAD_PROC:
            procedure = Gimp.LoadProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, load_spr, None)
            procedure.set_menu_label('Half-Life Sprite')
            procedure.set_extensions('spr')
            procedure.set_magics('0,string,IDSP')
            procedure.set_thumbnail_loader(LOAD_THUMB_PROC)
        elif name == LOAD_THUMB_PROC:
            procedure = Gimp.ThumbnailProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, load_spr_thumbnail, None)
        elif name == SAVE_PROC:
            procedure = Gimp.ExportProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, False, save_spr, None)
            procedure.set_menu_label('Half-Life Sprite')
            procedure.set_extensions('spr')
            procedure.set_image_types('*')

            procedure.add_int_argument(
                'sprite-type',
                'Sprite Type',
                'Sprite orientation, or -1 to use image metadata',
                -1,
                4,
                -1,
                GObject.ParamFlags.READWRITE,
            )

            procedure.add_int_argument(
                'texture-format',
                'Texture Format',
                'Normal (0), Additive (1), Indexalpha (2), Alphatest (3), or -1 for image metadata',
                -1,
                3,
                -1,
                GObject.ParamFlags.READWRITE,
            )

            procedure.add_string_aux_argument(
                'frame-settings',
                'Frame Settings',
                'Selected frames, order, and origins for repeat export',
                '',
                GObject.ParamFlags.READWRITE,
            )
        else:
            return None

        procedure.set_documentation(
            'Load or export Half-Life sprites (.spr)',
            'Import SPR v2 and embedded DDS textures in SPR v3; export SPR v2.',
            name,
        )
        procedure.set_attribution(AUTHOR, AUTHOR, '2020-2026')
        return procedure


if __name__ == '__main__':
    Gimp.main(HalfLifeSpritePlugin.__gtype__, sys.argv)
