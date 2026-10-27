"""Isolated Cycles emission baking. Original scenes and shader links are never edited."""

from array import array
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile

import bmesh
import bpy
import PyOpenColorIO as ocio


class BakeError(RuntimeError):
    pass


def default_uv(mesh):
    return next((uv.name for uv in mesh.uv_layers if uv.active_render),
                mesh.uv_layers.active.name if mesh.uv_layers.active else "")


def socket_by_id(sockets, identifier):
    return next((s for s in sockets if s.identifier == identifier), None)


def resolve_path(material, group_names, node_name):
    tree = material.node_tree
    groups = []
    for name in group_names:
        node = tree.nodes.get(name)
        if not node or node.type != 'GROUP' or not node.node_tree:
            raise BakeError("节点组路径已改变，请重新打开烘焙窗口")
        groups.append(node)
        tree = node.node_tree
    node = tree.nodes.get(node_name)
    if not node or node.type != 'TEX_IMAGE' or not node.image:
        raise BakeError("找不到原图像纹理节点，或节点没有图像")
    return tree, groups, node


def coordinate_sources(root_tree, groups, image_node):
    """Trace coordinates through mappings, reroutes, group inputs and outputs.

    Returns editable UV references plus unsupported coordinate origins. Group instances
    remain distinct even when they share a node tree.
    """
    references, unsupported, seen = [], [], set()

    def visit_input(tree, path, socket):
        if not socket.is_linked:
            if socket.node.type == 'TEX_IMAGE' and socket.name == 'Vector':
                references.append(('IMPLICIT', tree, socket.node, socket, path))
            return
        for link in socket.links:
            if link.is_valid:
                visit_output(tree, path, link.from_socket)

    def visit_output(tree, path, socket):
        key = (tuple(n.as_pointer() for n in path), socket.as_pointer())
        if key in seen:
            return
        seen.add(key)
        node = socket.node
        if node.type == 'UVMAP':
            if node.from_instancer:
                unsupported.append('实例 UV')
            else:
                references.append(('UVMAP', tree, node, socket, path))
        elif node.type == 'TEX_COORD':
            if socket.name == 'UV' and not node.from_instancer:
                references.append(('TEX_COORD', tree, node, socket, path))
            else:
                unsupported.append('纹理坐标 / ' + socket.name)
        elif node.type == 'GROUP' and node.node_tree:
            output = next((n for n in node.node_tree.nodes
                           if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
            inner = socket_by_id(output.inputs, socket.identifier) if output else None
            if inner:
                visit_input(node.node_tree, path + [node], inner)
        elif node.type == 'GROUP_INPUT':
            if path:
                outer = path[-1]
                outer_socket = socket_by_id(outer.inputs, socket.identifier)
                parent_tree = path[-2].node_tree if len(path) > 1 else root_tree
                if outer_socket:
                    visit_input(parent_tree, path[:-1], outer_socket)
            else:
                unsupported.append('未解析的组输入')
        else:
            if node.type in {'ATTRIBUTE', 'NEW_GEOMETRY', 'TEX_NOISE', 'TEX_VORONOI',
                             'TEX_MUSGRAVE', 'TEX_WAVE', 'TEX_GRADIENT', 'TEX_CHECKER'}:
                unsupported.append(node.bl_label)
            for input_socket in node.inputs:
                visit_input(tree, path, input_socket)

    tree = groups[-1].node_tree if groups else root_tree
    visit_input(tree, list(groups), image_node.inputs['Vector'])
    return references, sorted(set(unsupported))


def infer_input_uv(material, groups, node, mesh):
    refs, unsupported = coordinate_sources(material.node_tree, groups, node)
    fallback = default_uv(mesh)
    names = {ref[2].uv_map or fallback if ref[0] == 'UVMAP' else fallback
             for ref in refs}
    if not unsupported and len(names) == 1 and next(iter(names)) in mesh.uv_layers:
        return next(iter(names)), "已从节点坐标连接识别输入 UV"
    if unsupported:
        return fallback, "坐标包含非 UV 来源；可关闭“保留坐标处理”后直接使用输入 UV"
    if len(names) > 1:
        return fallback, "坐标使用多个 UV；所选输入 UV 将统一用于这些坐标来源"
    return fallback, "未识别到 UV 来源，默认使用模型渲染 UV"


def remap_coordinates(material, groups, node, uv_name, keep_mapping):
    tree = groups[-1].node_tree if groups else material.node_tree
    if not keep_mapping:
        uv = tree.nodes.new('ShaderNodeUVMap')
        uv.uv_map = uv_name
        tree.links.new(uv.outputs['UV'], node.inputs['Vector'])
        return
    refs, unsupported = coordinate_sources(material.node_tree, groups, node)
    if unsupported or not refs:
        raise BakeError("上游坐标不是可识别的 UV；请关闭“保留坐标处理”后重试")
    for kind, ref_tree, ref_node, socket, _path in refs:
        if kind == 'UVMAP':
            ref_node.uv_map = uv_name
        else:
            uv = ref_tree.nodes.new('ShaderNodeUVMap')
            uv.uv_map = uv_name
            if kind == 'IMPLICIT':
                ref_tree.links.new(uv.outputs['UV'], socket)
            else:
                # Replace this coordinate output only, leaving other outputs alone.
                for link in list(socket.links):
                    ref_tree.links.new(uv.outputs['UV'], link.to_socket)


def copy_material(material, owned_groups):
    result = material.copy()

    def copy_groups(tree, ancestors):
        for node in tree.nodes:
            if node.type == 'GROUP' and node.node_tree:
                if node.node_tree.as_pointer() in ancestors:
                    raise BakeError("不支持递归节点组")
                old = node.node_tree
                node.node_tree = old.copy()
                owned_groups.append(node.node_tree)
                copy_groups(node.node_tree, ancestors | {old.as_pointer()})

    try:
        copy_groups(result.node_tree, set())
        return result
    except Exception:
        bpy.data.materials.remove(result)
        raise


def expose_image(material, groups, node):
    """Expose the selected image's outputs through copies of any enclosing groups."""
    color, alpha = node.outputs['Color'], node.outputs['Alpha']
    for group in reversed(groups):
        tree = group.node_tree
        output = next((n for n in tree.nodes
                       if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
        if output is None:
            output = tree.nodes.new('NodeGroupOutput')
            output.is_active_output = True
        color_interface = tree.interface.new_socket(
            name='__UVBakeColor', in_out='OUTPUT', socket_type='NodeSocketColor')
        alpha_interface = tree.interface.new_socket(
            name='__UVBakeAlpha', in_out='OUTPUT', socket_type='NodeSocketFloat')
        tree.links.new(color, socket_by_id(output.inputs, color_interface.identifier))
        tree.links.new(alpha, socket_by_id(output.inputs, alpha_interface.identifier))
        color = socket_by_id(group.outputs, color_interface.identifier)
        alpha = socket_by_id(group.outputs, alpha_interface.identifier)
    tree = material.node_tree
    # Use one unambiguous ALL output; copied original outputs cannot win engine selection.
    for output in [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL']:
        tree.nodes.remove(output)
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    emission = tree.nodes.new('ShaderNodeEmission')
    tree.links.new(emission.outputs[0], output.inputs['Surface'])
    # Cycles premultiplies the Color output when Alpha has no consumer. Keep Alpha
    # connected while baking color, without changing emission strength.
    strength = tree.nodes.new('ShaderNodeMath')
    strength.operation = 'MULTIPLY_ADD'
    strength.inputs[1].default_value = 0.0
    strength.inputs[2].default_value = 1.0
    tree.links.new(alpha, strength.inputs[0])
    tree.links.new(strength.outputs[0], emission.inputs['Strength'])
    target = tree.nodes.new('ShaderNodeTexImage')
    for n in tree.nodes:
        n.select = n == target
    tree.nodes.active = target
    return emission, target, color, alpha


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:120]
    if name.lower().endswith('.png'):
        name = name[:-4]
    if not name:
        name = 'UVBake'
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'} | {
            f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)}:
        name = '_' + name
    return name


def save_unique_png(image, directory, basename):
    """Save first, then reserve a unique destination. Never overwrite another texture."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    basename = safe_filename(basename)
    fd, temporary = tempfile.mkstemp(prefix='.uvbake-', suffix='.png', dir=directory)
    os.close(fd)
    reserved = None
    try:
        image.file_format = 'PNG'
        image.filepath_raw = temporary
        image.save()
        for index in range(100000):
            suffix = f'_{index:03}' if index else ''
            candidate = directory / f'{basename}{suffix}.PNG'
            try:
                # Exclusive creation works on local and network volumes. Keeping the
                # handle open avoids Windows scanners racing a reserve/replace pair.
                with candidate.open('xb') as destination:
                    reserved = candidate
                    with open(temporary, 'rb') as source:
                        shutil.copyfileobj(source, destination)
                break
            except FileExistsError:
                continue
        if reserved is None:
            raise BakeError("输出目录中的同名文件过多")
        image.filepath_raw = str(reserved)
        return reserved
    except Exception:
        if reserved and reserved.exists():
            reserved.unlink()
        raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class BakeRequest:
    obj: object
    material: object
    node_name: str
    group_names: tuple
    input_uv: str
    output_uv: str
    width: int
    height: int
    margin: int = 8
    keep_mapping: bool = True
    bake_alpha: bool = True


def bake_image(context, request):
    """Return a new RGBA image. All temporary datablocks are cleaned even on error."""
    req = request
    obj, material = req.obj, req.material
    if obj.type != 'MESH' or obj.mode != 'OBJECT':
        raise BakeError("请选择网格物体，并切换到物体模式")
    if material.library or not material.is_editable:
        raise BakeError("请先将链接材质本地化")
    if req.width < 1 or req.height < 1 or req.width > 16384 or req.height > 16384:
        raise BakeError("图像尺寸须在 1–16384 之间")
    _, _, source = resolve_path(material, req.group_names, req.node_name)
    if source.image.source not in {'FILE', 'GENERATED'} or source.image.type not in {'IMAGE', 'UV_TEST'}:
        raise BakeError("当前仅支持单张图像；不支持 UDIM、序列、影片或渲染结果")
    if not source.image.has_data or min(source.image.size) < 1:
        raise BakeError("源图像未加载或文件丢失")
    if source.projection != 'FLAT':
        raise BakeError("UV 转换需要图像纹理节点使用平面（Flat）投影")
    indices = {i for i, slot in enumerate(obj.material_slots) if slot.material == material}
    if not indices:
        raise BakeError("此材质没有分配给当前物体")
    scene = mesh = duplicate = copied = result = color_image = alpha_image = None
    groups = []
    succeeded = False
    try:
        depsgraph = context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(
            obj.evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
        if req.input_uv not in mesh.uv_layers or req.output_uv not in mesh.uv_layers:
            raise BakeError("输入或输出 UV 不存在，或已被修改器移除")
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.material_index not in indices],
                             context='FACES')
            bm.to_mesh(mesh)
        finally:
            bm.free()
        if not len(mesh.polygons):
            raise BakeError("当前物体没有使用此材质的面")
        copied = copy_material(material, groups)
        mesh.materials.clear()
        mesh.materials.append(copied)
        for poly in mesh.polygons:
            poly.material_index = 0
        mesh.uv_layers[req.input_uv].active_render = True
        scene = bpy.data.scenes.new('__UVBakeScene')
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 1
        scene.cycles.use_denoising = False
        duplicate = bpy.data.objects.new('__UVBakeMesh', mesh)
        duplicate.matrix_world = obj.matrix_world.copy()
        scene.collection.objects.link(duplicate)
        layer = scene.view_layers[0]
        layer.objects.active = duplicate
        duplicate.select_set(True, view_layer=layer)
        _, copied_groups, node = resolve_path(copied, req.group_names, req.node_name)
        remap_coordinates(copied, copied_groups, node, req.input_uv, req.keep_mapping)
        emission, target, color, alpha = expose_image(copied, copied_groups, node)

        def new_target(name):
            image = bpy.data.images.new(name, width=req.width, height=req.height,
                                        alpha=True, float_buffer=True, is_data=True)
            image.generated_color = (0, 0, 0, 0)
            return image

        def bake(socket, image):
            copied.node_tree.links.new(socket, emission.inputs['Color'])
            target.image = image
            with context.temp_override(scene=scene, view_layer=layer, object=duplicate,
                                       active_object=duplicate, selected_objects=[duplicate],
                                       selected_editable_objects=[duplicate]):
                status = bpy.ops.object.bake(type='EMIT', target='IMAGE_TEXTURES',
                    save_mode='INTERNAL', use_selected_to_active=False, use_clear=True,
                    margin=req.margin, margin_type='EXTEND', uv_layer=req.output_uv)
            if 'FINISHED' not in status:
                raise BakeError("Cycles 未能完成烘焙")

        color_image = new_target('__UVBakeColor')
        bake(color, color_image)
        pixels = array('f', [0.0]) * (req.width * req.height * 4)
        color_image.pixels.foreach_get(pixels)
        # Convert scene-linear bake values into the source encoding before the single
        # final 8-bit quantization. Baking directly into a byte image loses dark detail.
        config = ocio.GetCurrentConfig()
        colorspace = config.getColorSpace(source.image.colorspace_settings.name)
        if colorspace is None:
            raise BakeError('无法识别源图像色彩空间')
        if not colorspace.isData():
            config.getProcessor(ocio.ROLE_SCENE_LINEAR, colorspace.getName()).getDefaultCPUProcessor().applyRGBA(pixels)
        if req.bake_alpha:
            alpha_image = new_target('__UVBakeAlpha')
            bake(alpha, alpha_image)
            alpha_pixels = array('f', [0.0]) * len(pixels)
            alpha_image.pixels.foreach_get(alpha_pixels)
            for i in range(3, len(pixels), 4):
                pixels[i] = alpha_pixels[i - 3]
        # Byte image preserves ordinary PNG precision; Image.save uses its color space,
        # avoiding Filmic/AgX transforms from a user's render settings.
        result = bpy.data.images.new(source.image.name + '_' + req.output_uv,
            width=req.width, height=req.height, alpha=True, float_buffer=False)
        result.colorspace_settings.name = source.image.colorspace_settings.name
        result.alpha_mode = 'CHANNEL_PACKED' if source.image.alpha_mode == 'CHANNEL_PACKED' else 'STRAIGHT'
        result.pixels.foreach_set(pixels)
        result.update()
        succeeded = True
        return result
    finally:
        if duplicate:
            bpy.data.objects.remove(duplicate, do_unlink=True)
        if scene:
            bpy.data.scenes.remove(scene)
        if mesh:
            bpy.data.meshes.remove(mesh)
        if copied:
            bpy.data.materials.remove(copied)
        for group in groups:
            bpy.data.node_groups.remove(group, do_unlink=True)
        for image in (color_image, alpha_image, result if not succeeded else None):
            if image:
                bpy.data.images.remove(image, do_unlink=True)


def add_result_nodes(tree, source, image, output_uv):
    """Place beside the source in the same frame; never reconnect the material."""
    created = []
    selected = [n for n in tree.nodes if n.select]
    active = tree.nodes.active
    try:
        uv = tree.nodes.new('ShaderNodeUVMap')
        created.append(uv)
        uv.uv_map = output_uv
        uv.label = '输出 UV · ' + output_uv
        uv.parent = source.parent
        uv.location = (source.location.x + source.width + 70, source.location.y - 330)
        texture = tree.nodes.new('ShaderNodeTexImage')
        created.append(texture)
        texture.parent = source.parent
        texture.location = (source.location.x + source.width + 290, source.location.y)
        texture.image = image
        texture.label = '烘焙 · ' + output_uv
        texture.width = source.width
        texture.interpolation = source.interpolation
        texture.extension = source.extension
        tree.links.new(uv.outputs['UV'], texture.inputs['Vector'])
        for node in tree.nodes:
            node.select = node == texture
        tree.nodes.active = texture
        return texture
    except Exception:
        for node in reversed(created):
            tree.nodes.remove(node)
        for node in selected:
            node.select = True
        tree.nodes.active = active
        raise
