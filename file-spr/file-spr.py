#!/usr/bin/env python2
# GIMP plugin for the Half-Life sprite format (.spr)

# TODO: Full support group frames

from gimpfu import *
import gimpui
import os, sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from struct import pack, unpack
from spr import Sprite

t = gettext.translation('gimp20-python', gimp.locale_directory, fallback=True)
ugettext = t.ugettext

AUTHOR = 'Psycrow'
COPYRIGHT = AUTHOR
COPYRIGHT_YEAR = '2020'

EDITOR_PROC = 'hl-spr-export-dialog'
LOAD_PROC = 'file-hl-spr-load'
LOAD_THUMB_PROC  = 'file-hl-spr-load-thumb'
SAVE_PROC = 'file-hl-spr-save'


def load_spr_thumbnail(file_path, thumb_size):
    img = Sprite.load_from_file(file_path, True)[0]
    width, height = img.width, img.height
    scale = float(thumb_size) / max(width, height)
    if scale and scale != 1.0:
        width = int(width * scale)
        height = int(height * scale)
        pdb.gimp_image_scale(img, width, height)

    return (img, width, height)


def load_spr(file_path, raw_filename):
    try:
        images = Sprite.load_from_file(file_path)
        for img in images[:-1]:
            gimp.Display(img)
            gimp.displays_flush()
        return images[-1]
    except Exception as e:
        fail('Error loading sprite file:\n\n%s!' % e.message)


def save_spr(img, drawable, filename, raw_filename):
    from array import array
    import pygtk
    import gtk
    pygtk.require('2.0')

    THUMB_MAXSIZE = 128
    RESPONSE_EXPORT = 1
    MIN_FRAME_ORIGIN = -8192
    MAX_FRAME_ORIGIN = 8192
    LS_LAYER, LS_PIXBUF, LS_SIZE_INFO, LS_EXPORT, LS_ORIGIN_X, LS_ORIGIN_Y, LS_THUMBDATA = range(7)

    spr_img = img.duplicate()
    gimpui.gimp_ui_init()

    if spr_img.base_type != INDEXED:
        try:
            pdb.gimp_convert_indexed(spr_img, NO_DITHER, MAKE_PALETTE, 256, 0, 0, '')
        except RuntimeError:
            # Gimp does not support indexed mode if the image contains layer groups, so delete them
            for layer in spr_img.layers:
                if pdb.gimp_item_is_group(layer):
                    pdb.gimp_image_merge_layer_group(spr_img, layer)
            pdb.gimp_convert_indexed(spr_img, NO_DITHER, MAKE_PALETTE, 256, 0, 0, '')

    def make_thumbnail_data(layer):
        width = layer.width
        height = layer.height

        indices = layer.get_pixel_rgn(0, 0, width, height)[:, :]
        if layer.type == INDEXEDA_IMAGE:
            indices = indices[::2]
        indices = ''.join(i + i for i in indices)

        thumbnail_layer = gimp.Layer(spr_img, layer.name + '_temp', width, height, INDEXEDA_IMAGE, 100, NORMAL_MODE)
        thumbnail_rgn = thumbnail_layer.get_pixel_rgn(0, 0, width, height)
        thumbnail_rgn[:, :] = indices
        pdb.gimp_image_insert_layer(spr_img, thumbnail_layer, None, 0)

        scale = float(THUMB_MAXSIZE) / max(width, height)
        if scale < 1.0:
            width = max(int(width * scale), 1)
            height = max(int(height * scale), 1)
            pdb.gimp_layer_scale_full(thumbnail_layer, width, height, False, 0)

        width, height, bpp, unused_, tn_data = pdb.gimp_drawable_thumbnail(thumbnail_layer, width, height)
        spr_img.remove_layer(thumbnail_layer)
        return str(bytearray(tn_data)), width, height, bpp

    def get_thumbnail(thumbnail_data, texture_format):
        tn_data, width, height, bpp = thumbnail_data

        last_index = len(spr_img.colormap) // 3 - 1

        if texture_format != Sprite.TEXTURE_FORMAT_INDEXALPHA:
            tn_data = array('B', tn_data)
            if texture_format == Sprite.TEXTURE_FORMAT_ADDITIVE:
                for i in xrange(0, len(tn_data), 4):
                    tn_data[i + 3] = sum(tn_data[i:i + 3]) // 3
            elif texture_format == Sprite.TEXTURE_FORMAT_ALPHATEST:
                for i in xrange(0, len(tn_data), 4):
                    tn_data[i + 3] = 0xff - (tn_data[i + 3] // last_index * 0xff)
            else:
                for i in xrange(0, len(tn_data), 4):
                    tn_data[i + 3] = 0xff
            tn_data = tn_data.tostring()

        return gtk.gdk.pixbuf_new_from_data(
            tn_data,
            gtk.gdk.COLORSPACE_RGB,
            True,
            8,
            width, height,
            width * bpp)

    class ExportDialog(gimpui.Dialog):

        def __init__(self):
            gimpui.Dialog.__init__(self, title=ugettext('Export Image as Half-Life sprite'),
                                   role=EDITOR_PROC, help_id=None,
                                   buttons=(gtk.STOCK_CANCEL, gtk.RESPONSE_CLOSE,
                                            ugettext('Export'), RESPONSE_EXPORT))

            self.set_name(EDITOR_PROC)
            self.connect('response', self.on_response)
            self.connect('destroy', self.on_destroy)

            export_opt_box = self.make_export_options_box()
            self.img_view_frame = self.make_frames_view(reversed(spr_img.layers))

            hbox = gtk.HBox()
            hbox.pack_start(export_opt_box, True, True, 20)
            hbox.pack_start(self.img_view_frame, True, True, 5)

            self.vbox.pack_start(hbox)
            self.vbox.show_all()

            self.set_resizable(False)
            self.get_widget_for_response(RESPONSE_EXPORT).grab_focus()

        def update_thumbnails(self):
            texture_format = self.cb_tf.get_active()
            for ls in self.liststore:
                ls[LS_PIXBUF] = get_thumbnail(ls[LS_THUMBDATA], texture_format)

        def make_export_options_box(self):
            # Sprite type
            spr_type = spr_img.parasite_find('spr_type')
            self.cb_st = gtk.combo_box_new_text()
            self.cb_st.append_text('VP Parallel Upright')
            self.cb_st.append_text('Facing Upright')
            self.cb_st.append_text('VP Parallel')
            self.cb_st.append_text('Oriented')
            self.cb_st.append_text('VP Parallel Oriented')
            self.cb_st.set_tooltip_text(ugettext('Sprite Type'))
            self.cb_st.set_active(spr_type.flags if spr_type else 0)

            box = gtk.VBox(True, 5)
            box.pack_start(self.cb_st, False, False)

            st_frame = gimpui.Frame('Sprite Type:')
            st_frame.set_shadow_type(gtk.SHADOW_IN)
            st_frame.add(box)

            # Texture format
            texture_format = spr_img.parasite_find('spr_format')
            self.cb_tf = gtk.combo_box_new_text()
            self.cb_tf.append_text('Normal')
            self.cb_tf.append_text('Additive')
            self.cb_tf.append_text('Indexalpha')
            self.cb_tf.append_text('Alphatest')
            self.cb_tf.set_tooltip_text(ugettext('Texture Format'))
            self.cb_tf.set_active(texture_format.flags if texture_format else 0)

            def cb_tf_changed(cb):
                self.update_thumbnails()

            self.cb_tf.connect('changed', cb_tf_changed)

            box = gtk.VBox(True, 5)
            box.pack_start(self.cb_tf, False, False)

            tf_frame = gimpui.Frame('Texture Format:')
            tf_frame.set_shadow_type(gtk.SHADOW_IN)
            tf_frame.add(box)

            # Add origins offset
            lbl_oo_x = gtk.Label('Origin X:')

            adjustment = gtk.Adjustment(lower=MIN_FRAME_ORIGIN, upper=MAX_FRAME_ORIGIN, step_incr=1)
            self.sb_oo_x = gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
            self.sb_oo_x.set_tooltip_text(ugettext('Offset for origin X'))

            box_origin_x = gtk.HBox(True, 12)
            box_origin_x.pack_start(lbl_oo_x, False, False)
            box_origin_x.pack_start(self.sb_oo_x, False, False)

            lbl_oo_y = gtk.Label('Origin Y:')

            adjustment = gtk.Adjustment(lower=MIN_FRAME_ORIGIN, upper=MAX_FRAME_ORIGIN, step_incr=1)
            self.sb_oo_y = gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
            self.sb_oo_y.set_tooltip_text(ugettext('Offset for origin Y'))

            box_origin_y = gtk.HBox(True, 12)
            box_origin_y.pack_start(lbl_oo_y, False, False)
            box_origin_y.pack_start(self.sb_oo_y, False, False)

            def btn_oo_clicked(btn):
                offset_x = self.sb_oo_x.get_value_as_int()
                offset_y = self.sb_oo_y.get_value_as_int()
                for ls in self.liststore:
                    ls[LS_ORIGIN_X] += offset_x
                    ls[LS_ORIGIN_Y] += offset_y

            btn_oo = gtk.Button('Add offsets')
            btn_oo.connect('clicked', btn_oo_clicked)

            box = gtk.VBox(True, 5)
            box.pack_start(box_origin_x, False, False)
            box.pack_start(box_origin_y, False, False)
            box.pack_start(btn_oo, False, False)

            oo_frame = gimpui.Frame('Add origin offsets:')
            oo_frame.set_shadow_type(gtk.SHADOW_IN)
            oo_frame.add(box)

            # Main option frame
            o_box = gtk.VBox()
            o_box.set_size_request(110, -1)
            o_box.pack_start(st_frame, False, False, 10)
            o_box.pack_start(tf_frame, False, False, 10)
            o_box.pack_start(oo_frame, False, False, 10)

            box = gtk.VBox()
            box.set_size_request(140, -1)
            box.pack_start(o_box, True, False)

            return box

        def make_frames_view(self, layers):
            import gobject

            texture_format = self.cb_tf.get_active()
            self.liststore = gtk.ListStore(gobject.TYPE_PYOBJECT, gtk.gdk.Pixbuf, str, gobject.TYPE_BOOLEAN,
                                           gobject.TYPE_INT, gobject.TYPE_INT, gobject.TYPE_PYOBJECT)
            for l in layers:
                frames = [gl for gl in reversed(l.layers)] if pdb.gimp_item_is_group(l) else [l]
                for f in frames:
                    thumbnail_data = make_thumbnail_data(f)
                    pixbuf = get_thumbnail(thumbnail_data, texture_format)
                    size_info = '<b>Size</b>: %d x %d' % (f.width, f.height)
                    parasite_origins = f.parasite_find('spr_origins')
                    if parasite_origins:
                        origin_x, origin_y = unpack('<2i', parasite_origins.data[:8])
                    else:
                        origin_x, origin_y = -f.width // 2, f.height // 2
                    self.liststore.append([f, pixbuf, size_info, True, origin_x, origin_y, thumbnail_data])

            self.export_frames_num = len(self.liststore)
            self.iconview = gtk.TreeView(self.liststore)
            self.iconview.set_reorderable(True)

            self.iconview.set_enable_search(False)
            self.iconview.set_grid_lines(gtk.TREE_VIEW_GRID_LINES_BOTH)

            # Column 'Export'
            def on_cb_export_toggled(widget, path):
                export = not self.liststore[path][LS_EXPORT]
                self.liststore[path][LS_EXPORT] = export
                self.export_frames_num += 1 if export else -1
                self.set_btn_export_sensitive(self.export_frames_num > 0)
                self.img_view_frame.set_label('Frames to export: %d' % self.export_frames_num)

            cb_export = gtk.CellRendererToggle()
            cb_export.connect('toggled', on_cb_export_toggled)

            col_export = gtk.TreeViewColumn('Export', cb_export, active=LS_EXPORT)
            col_export_header = gtk.Label('Export')
            col_export_header.show()

            tt_export = gtk.Tooltips()
            tt_export.set_tip(col_export_header, 'Export frame to file.')

            col_export.set_sort_order(gtk.SORT_DESCENDING)
            col_export.set_sort_column_id(4)

            col_export.set_widget(col_export_header)
            self.iconview.append_column(col_export)

            # Column 'Frame'
            pixrend = gtk.CellRendererPixbuf()
            col_pixbuf = gtk.TreeViewColumn('Frame', pixrend, pixbuf=LS_PIXBUF)
            col_pixbuf.set_min_width(THUMB_MAXSIZE)
            self.iconview.append_column(col_pixbuf)

            # Column 'Settings'
            col_info = gtk.TreeViewColumn()
            col_info_header = gtk.Label('Settings')
            col_info_header.show()
            col_info.set_widget(col_info_header)

            tt_info = gtk.Tooltips()
            tt_info.set_tip(col_info_header, 'Frame export options.')

            # Info text
            renderer = gtk.CellRendererText()
            renderer.set_property('yalign', 0.3)
            renderer.set_property('xalign', 0.0)
            renderer.set_property('width', 0)
            renderer.set_property('height', THUMB_MAXSIZE)
            col_info.pack_start(renderer, False)
            col_info.set_attributes(renderer, markup=LS_SIZE_INFO)

            # Label origin X
            adjustment = gtk.Adjustment(lower=MIN_FRAME_ORIGIN, upper=MAX_FRAME_ORIGIN, step_incr=1)
            renderer = gtk.CellRendererText()
            renderer.set_property('markup', '<b>Origin X</b>:')
            renderer.set_property('width', 64)
            col_info.pack_start(renderer, False)

            # crs origin x
            adjustment = gtk.Adjustment(lower=MIN_FRAME_ORIGIN, upper=MAX_FRAME_ORIGIN, step_incr=1)
            renderer = gtk.CellRendererSpin()
            renderer.set_property('editable', True)
            renderer.set_property('adjustment', adjustment)

            def on_crs_origin_x_changed(widget, path, val):
                val = min(max(int(val), MIN_FRAME_ORIGIN), MAX_FRAME_ORIGIN)
                self.liststore[path][LS_ORIGIN_X] = val

            renderer.connect('edited', on_crs_origin_x_changed)

            col_info.pack_start(renderer)
            col_info.set_attributes(renderer, markup=LS_ORIGIN_X)

            # Label origin y
            renderer = gtk.CellRendererText()
            renderer.set_property('markup', '<b>Origin Y</b>:')
            renderer.set_property('width', 64)
            col_info.pack_start(renderer, False)

            # crs origin y
            renderer = gtk.CellRendererSpin()
            renderer.set_property('editable', True)
            renderer.set_property('adjustment', adjustment)

            def on_crs_origin_x_changed(widget, path, val):
                val = min(max(int(val), MIN_FRAME_ORIGIN), MAX_FRAME_ORIGIN)
                self.liststore[path][LS_ORIGIN_Y] = val

            renderer.connect('edited', on_crs_origin_x_changed)

            col_info.pack_start(renderer)
            col_info.set_attributes(renderer, markup=LS_ORIGIN_Y)

            self.iconview.append_column(col_info)

            scrl_win = gtk.ScrolledWindow()
            scrl_win.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
            scrl_win.add(self.iconview)
            scrl_win.set_size_request(THUMB_MAXSIZE, THUMB_MAXSIZE * 4)

            frame_imgs = gimpui.Frame('Frames to export: %d' % self.export_frames_num)
            frame_imgs.set_property('label-xalign', 0.05)
            frame_imgs.set_shadow_type(gtk.SHADOW_IN)
            frame_imgs.add(scrl_win)
            frame_imgs.set_size_request(535, -1)

            return frame_imgs

        def export_selected_frames(self):
            layers = []
            for row in self.liststore:
                if not row[LS_EXPORT]:
                    continue

                layer = row[LS_LAYER]
                origins_data = pack('<2i', row[LS_ORIGIN_X], row[LS_ORIGIN_Y])
                if layer.parasite_find('spr_origins'):
                    layer.parasite_detach('spr_origins')
                layer.attach_new_parasite('spr_origins', 0, origins_data)

                layers.append(layer)

            # Make grouped layers with parasites
            grouped_layers, added_layers = [], []
            for layer in layers:
                if layer in added_layers:
                    continue

                added_layers.append(layer)

                parent = layer.parent
                if not parent:
                    grouped_layers.append([layer])
                    continue

                if not parent.parasite_find('spr_type'):
                    parent.attach_new_parasite('spr_type', 1, '')

                group_lst = [ll for ll in layers if ll.parent == parent]
                for i, ll in enumerate(group_lst):
                    interval = ll.parasite_find('spr_interval')
                    if not interval:
                        ll.attach_new_parasite('spr_interval', 0, pack('<f', (i + 1) * 0.1))

                    added_layers.append(ll)
                grouped_layers.append([parent] + group_lst)

            # Export to file
            if grouped_layers:
                Sprite.save_to_file(spr_img, filename, grouped_layers,
                                    self.cb_st.get_active(),
                                    self.cb_tf.get_active())

        def set_btn_export_sensitive(self, sensitive):
            self.get_widget_for_response(RESPONSE_EXPORT).set_sensitive(sensitive)

        def on_response(self, dialog, response_id):
            self.destroy()
            while gtk.events_pending():
                gtk.main_iteration()

            if response_id == RESPONSE_EXPORT:
                self.export_selected_frames()
                
        def on_destroy(self, widget):
            gtk.main_quit()

        def run(self):
            self.show()
            gtk.main()

    ExportDialog().run()
    pdb.gimp_image_delete(spr_img)


def register_load_handlers():
    gimp.register_magic_load_handler(LOAD_PROC, 'spr', '', '0,string,' + str(Sprite.MAGIC))
    pdb.gimp_register_thumbnail_loader(LOAD_PROC, LOAD_THUMB_PROC)


def register_save_handlers():
    gimp.register_save_handler(SAVE_PROC, 'spr', '')


register(
    LOAD_THUMB_PROC,
    'Loads a thumbnail for Half-Life sprite (.spr)',
    '',
    AUTHOR,
    COPYRIGHT,
    COPYRIGHT_YEAR,
    None,
    None,
    [
        (PF_STRING, 'filename', 'The name of the file to load', None),
        (PF_INT, 'thumb-size', 'Preferred thumbnail size', None),
    ],
    [
        (PF_IMAGE, 'image', 'Thumbnail image'),
        (PF_INT, 'image-width', 'Width of full-sized image'),
        (PF_INT, 'image-height', 'Height of full-sized image')
    ],
    load_spr_thumbnail,
    run_mode_param = False
)

register(
    LOAD_PROC,
    'Loads Half-Life sprite (.spr)',
    '',
    AUTHOR,
    COPYRIGHT,
    COPYRIGHT_YEAR,
    'Half-Life sprite',
    None,
    [
        (PF_STRING, 'filename', 'The name of the file to load', None),
        (PF_STRING, 'raw-filename', 'The name entered', None),
    ],
    [(PF_IMAGE, 'image', 'Output image')],
    load_spr,
    on_query=register_load_handlers,
    menu='<Load>'
)

register(
    SAVE_PROC,
    'Saves Half-Life sprite (.spr)',
    '',
    AUTHOR,
    COPYRIGHT,
    COPYRIGHT_YEAR,
    'Half-Life sprite',
    '*',
    [
        (PF_IMAGE, 'image', 'Input image', None),
        (PF_DRAWABLE, 'drawable', 'Input drawable', None),
        (PF_STRING, 'filename', 'The name of the file', None),
        (PF_STRING, 'raw-filename', 'The name of the file', None),
    ],
    [],
    save_spr,
    on_query=register_save_handlers,
    menu='<Save>'
)

main()
