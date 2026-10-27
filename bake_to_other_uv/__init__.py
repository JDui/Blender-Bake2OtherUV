"""Bake to Other UV — Blender 5.2 LTS extension."""

import json
from pathlib import Path
import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from .core import (BakeError, BakeRequest, add_result_nodes, bake_image, default_uv,
                   infer_input_uv, resolve_path, safe_filename, save_unique_png)


def default_output_name(image_name, uv_name):
    # Strip actual image extensions while retaining dots in names such as Face.v2.
    suffix = Path(image_name).suffix
    if suffix.lower() in {'.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff', '.exr',
                          '.hdr', '.webp', '.bmp', '.dds', '.jp2'}:
        image_name = image_name[:-len(suffix)]
    return safe_filename(image_name + '_' + uv_name) + '.PNG'


def update_output_name(self, context):
    if not self.source_image_name:
        return
    suggested = default_output_name(self.source_image_name, self.output_uv)
    if not self.filename or self.filename == self.automatic_filename:
        self.filename = suggested
    self.automatic_filename = suggested


def editor_source(context):
    space = context.space_data
    if not space or space.type != 'NODE_EDITOR' or space.tree_type != 'ShaderNodeTree':
        return None
    if space.shader_type != 'OBJECT':
        return None
    obj = context.object
    material = space.id
    if material is None and not space.pin and obj:
        material = obj.active_material
    if not isinstance(material, bpy.types.Material):
        return None
    tree = space.edit_tree or material.node_tree
    node = tree.nodes.active if tree else None
    if not node or node.type != 'TEX_IMAGE' or not obj or obj.type != 'MESH':
        return None
    # Derive the concrete instance path, including shared/nested group instances.
    path = list(space.path)
    group_names = []
    for parent, child in zip(path, path[1:]):
        group = parent.node_tree.nodes.active
        if not group or group.type != 'GROUP' or group.node_tree != child.node_tree:
            matches = [n for n in parent.node_tree.nodes
                       if n.type == 'GROUP' and n.node_tree == child.node_tree]
            if len(matches) != 1:
                return None
            group = matches[0]
        group_names.append(group.name)
    return obj, material, tree, node, group_names


class NODE_OT_bake_to_other_uv(bpy.types.Operator):
    bl_idname = 'node.bake_to_other_uv'
    bl_label = '烘焙到其他UV'
    bl_description = '将当前图像纹理按另一套 UV 烘焙为 PNG，可在旁边创建输出节点'
    # Disk writes cannot be undone; node/image additions participate in Blender undo.
    bl_options = {'REGISTER', 'UNDO'}

    object_name: StringProperty(options={'HIDDEN'})
    material_name: StringProperty(options={'HIDDEN'})
    node_name: StringProperty(options={'HIDDEN'})
    group_path: StringProperty(default='[]', options={'HIDDEN'})
    input_uv: StringProperty(name='输入 UV', description='源图像采样所使用的 UV')
    output_uv: StringProperty(name='输出 UV', description='烘焙图像展开使用的目标 UV',
                             update=update_output_name)
    output_mode: EnumProperty(name='输出方式', items=[
        ('PNG', '导出 PNG', '保存 PNG 文件'),
        ('IMPORT', '导入回 Blender', '保存到工程旁的 TEX 目录，并在原节点旁创建新节点')], default='PNG')
    directory: StringProperty(name='输出目录', subtype='DIR_PATH')
    filename: StringProperty(name='输出名称',
        description='默认：当前贴图名_输出UV名.PNG；可自行修改，省略扩展名时自动补全；重名时自动编号')
    source_image_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    automatic_filename: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    width: IntProperty(name='宽度', default=1024, min=1, max=16384, subtype='PIXEL')
    height: IntProperty(name='高度', default=1024, min=1, max=16384, subtype='PIXEL')
    margin: IntProperty(name='边缘扩展', description='在 UV 岛外延展的像素数',
                        default=8, min=0, max=256, subtype='PIXEL')
    bake_alpha: BoolProperty(name='保留透明通道', description='额外烘焙 Alpha，保留原图透明度', default=True)
    keep_mapping: BoolProperty(name='保留坐标处理',
        description='保留上游 Mapping 等坐标运算；关闭后用所选输入 UV 直接采样原图', default=True)
    uv_hint: StringProperty(options={'HIDDEN'})
    last_output: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        source = editor_source(context)
        return bool(source and source[3].image and source[0].mode == 'OBJECT'
                    and len(source[0].data.uv_layers))

    def invoke(self, context, event):
        source = editor_source(context)
        if not source:
            self.report({'ERROR'}, '请在网格物体的材质中选择图像纹理节点')
            return {'CANCELLED'}
        obj, material, _tree, node, names = source
        if not node.image:
            self.report({'ERROR'}, '所选节点尚未指定图像')
            return {'CANCELLED'}
        self.object_name, self.material_name = obj.name, material.name
        self.node_name, self.group_path = node.name, json.dumps(names)
        _, groups, _ = resolve_path(material, names, node.name)
        self.input_uv, self.uv_hint = infer_input_uv(material, groups, node, obj.data)
        self.source_image_name = ''
        self.output_uv = default_uv(obj.data)
        self.width, self.height = node.image.size
        self.source_image_name = node.image.name
        self.filename = default_output_name(self.source_image_name, self.output_uv)
        self.automatic_filename = self.filename
        self.directory = str(Path(bpy.data.filepath).parent / 'TEX') if bpy.data.filepath else str(Path.home() / 'Pictures')
        return context.window_manager.invoke_props_dialog(self, width=500, confirm_text='开始烘焙')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        obj = bpy.data.objects.get(self.object_name)
        if not obj:
            layout.label(text='原物体已删除，请重新打开窗口', icon='ERROR')
            return
        layout.label(text=self.node_name, icon='TEXTURE')
        layout.prop_search(self, 'input_uv', obj.data, 'uv_layers')
        layout.prop_search(self, 'output_uv', obj.data, 'uv_layers')
        layout.label(text=self.uv_hint, icon='INFO')
        layout.prop(self, 'keep_mapping')
        layout.separator()
        layout.prop(self, 'output_mode')
        if self.output_mode == 'PNG':
            layout.prop(self, 'directory')
        else:
            if bpy.data.filepath:
                layout.label(text=str(Path(bpy.data.filepath).parent / 'TEX'), icon='FILE_FOLDER')
            else:
                layout.label(text='导入回 Blender 前，请先保存 .blend 工程', icon='ERROR')
        layout.prop(self, 'filename')
        row = layout.row(align=True)
        row.prop(self, 'width')
        row.prop(self, 'height')
        layout.prop(self, 'margin')
        layout.prop(self, 'bake_alpha')
        layout.separator()
        layout.label(text='仅烘焙当前物体中使用此材质的面；重名文件自动编号', icon='INFO')
        layout.label(text='Cycles CPU 烘焙期间请稍候，输出为 8 位 RGBA PNG')

    def execute(self, context):
        image = None
        saved = None
        cursor_changed = False
        try:
            if not all((self.object_name, self.material_name, self.node_name)):
                raise BakeError('烘焙参数尚未初始化，请从图像纹理节点右键菜单重新打开设置窗口')
            obj = bpy.data.objects.get(self.object_name)
            material = bpy.data.materials.get(self.material_name)
            if not obj or not material:
                raise BakeError('原物体或材质已删除，请重新打开烘焙窗口')
            if material.library or not material.is_editable:
                raise BakeError('请先将链接材质本地化')
            names = tuple(json.loads(self.group_path))
            tree, _groups, source = resolve_path(material, names, self.node_name)
            if self.output_mode == 'IMPORT':
                if not bpy.data.filepath:
                    raise BakeError('请先保存 .blend 工程，再选择导入回 Blender')
                if not tree.is_editable:
                    raise BakeError('输出节点所在节点组不可编辑，请先将其本地化')
                directory = Path(bpy.data.filepath).parent / 'TEX'
            else:
                if not self.directory.strip():
                    raise BakeError('请选择 PNG 输出目录')
                if self.directory.startswith('//') and not bpy.data.filepath:
                    raise BakeError('请保存工程或选择绝对输出目录')
                directory = Path(bpy.path.abspath(self.directory))
            # Detect unwritable directories before the expensive bake.
            directory.mkdir(parents=True, exist_ok=True)
            import tempfile
            with tempfile.TemporaryFile(dir=directory):
                pass
            if context.window:
                context.window.cursor_set('WAIT')
                cursor_changed = True
            request = BakeRequest(obj, material, self.node_name, names, self.input_uv,
                self.output_uv, self.width, self.height, self.margin,
                self.keep_mapping, self.bake_alpha)
            image = bake_image(context, request)
            saved = save_unique_png(image, directory, self.filename)
            image.name = saved.name
            if self.output_mode == 'IMPORT':
                if bpy.data.filepath:
                    image.filepath_raw = bpy.path.relpath(str(saved))
                add_result_nodes(tree, source, image, self.output_uv)
            else:
                bpy.data.images.remove(image)
                image = None
            self.last_output = str(saved)
            self.report({'INFO'}, '烘焙完成：' + str(saved))
            return {'FINISHED'}
        except Exception as exc:
            if image:
                bpy.data.images.remove(image, do_unlink=True)
            # If node creation failed, this operation owns the unique saved file.
            if saved and saved.exists():
                saved.unlink()
            if not isinstance(exc, BakeError):
                traceback.print_exc()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            if cursor_changed:
                context.window.cursor_set('DEFAULT')


def draw_context_menu(self, context):
    if editor_source(context):
        self.layout.separator()
        # Node context menus start in EXEC_REGION_WIN. Always invoke the dialog so
        # its source object/material/node and UV defaults are captured before execute.
        row = self.layout.row()
        row.operator_context = 'INVOKE_DEFAULT'
        row.operator(NODE_OT_bake_to_other_uv.bl_idname, icon='RENDER_STILL')


def register():
    bpy.utils.register_class(NODE_OT_bake_to_other_uv)
    bpy.types.NODE_MT_context_menu.append(draw_context_menu)


def unregister():
    bpy.types.NODE_MT_context_menu.remove(draw_context_menu)
    bpy.utils.unregister_class(NODE_OT_bake_to_other_uv)


if __name__ == '__main__':
    register()
