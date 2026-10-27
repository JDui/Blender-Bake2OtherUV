"""Run with Blender --background --factory-startup --python this_file.py."""
from pathlib import Path
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bake_to_other_uv as addon
from bake_to_other_uv import core

OUT = ROOT / 'tests' / 'output'
OUT.mkdir(parents=True, exist_ok=True)
PASSED = []


def check(name, function):
    function()
    PASSED.append(name)
    print('PASS:', name, flush=True)


def counts():
    return tuple(len(getattr(bpy.data, key)) for key in
                 ('scenes', 'objects', 'meshes', 'materials', 'node_groups', 'images'))


def sample(image, x, y):
    width, height = image.size
    offset = (int(y * height) * width + int(x * width)) * 4
    return tuple(image.pixels[offset:offset + 4])


def close(actual, expected, tolerance=0.016):
    assert max(abs(a - b) for a, b in zip(actual, expected)) < tolerance, (actual, expected)


def setup(colorspace='Non-Color'):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = bpy.data.meshes.new('TestMesh')
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    uv = mesh.uv_layers.new(name='SourceUV')
    target = mesh.uv_layers.new(name='TargetUV')
    for loop, co in zip(mesh.loops, [(0, 0), (1, 0), (1, 1), (0, 1)]):
        uv.data[loop.index].uv = co
        target.data[loop.index].uv = (1 - co[0], co[1])
    target.active_render = True
    mesh.uv_layers.active = uv
    obj = bpy.data.objects.new('TestObject', mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    mat = bpy.data.materials.new('TestMaterial')
    mat.use_nodes = True
    mesh.materials.append(mat)
    image = bpy.data.images.new('Input', width=32, height=32, alpha=True)
    image.colorspace_settings.name = colorspace
    image.alpha_mode = 'STRAIGHT'
    values = []
    for y in range(32):
        for x in range(32):
            values.extend((0.8 if x < 16 else 0.2, 0.15 if y < 16 else 0.65,
                           0.35, 0.25 if x < 16 else 0.75))
    image.pixels[:] = values
    image.update()
    node = mat.node_tree.nodes.new('ShaderNodeTexImage')
    node.image = image
    node.interpolation = 'Closest'
    node.extension = 'EXTEND'
    uv_node = mat.node_tree.nodes.new('ShaderNodeUVMap')
    uv_node.uv_map = 'SourceUV'
    mat.node_tree.links.new(uv_node.outputs['UV'], node.inputs['Vector'])
    mat.node_tree.nodes.active = node
    return obj, mat, node


def request(obj, mat, node, names=(), **kwargs):
    return core.BakeRequest(obj, mat, node.name, names, 'SourceUV', 'TargetUV', 32, 32,
                            margin=0, **kwargs)


def pixels_and_png(colorspace):
    obj, mat, node = setup(colorspace)
    state = counts()
    links = [(l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier)
             for l in mat.node_tree.links]
    result = core.bake_image(bpy.context, request(obj, mat, node))
    print('PIXELS', colorspace, sample(node.image, .25, .25),
          sample(result, .75, .25), flush=True)
    for x in (.25, .75):
        for y in (.25, .75):
            close(sample(result, 1 - x, y), sample(node.image, x, y))
    path = core.save_unique_png(result, OUT, colorspace)
    loaded = bpy.data.images.load(str(path), check_existing=False)
    loaded.colorspace_settings.name = colorspace
    for x in (.25, .75):
        for y in (.25, .75):
            close(sample(loaded, 1 - x, y), sample(node.image, x, y))
    assert path.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'
    assert [(l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier)
            for l in mat.node_tree.links] == links
    assert obj.data.uv_layers['TargetUV'].active_render
    assert bpy.context.view_layer.objects.active == obj and obj.select_get()
    bpy.data.images.remove(result)
    bpy.data.images.remove(loaded)
    assert counts() == state, (counts(), state)


def uv_defaults():
    obj, mat, node = setup()
    inferred, _ = core.infer_input_uv(mat, [], node, obj.data)
    assert inferred == 'SourceUV'
    assert core.default_uv(obj.data) == 'TargetUV'
    mat.node_tree.links.remove(node.inputs['Vector'].links[0])
    inferred, _ = core.infer_input_uv(mat, [], node, obj.data)
    assert inferred == 'TargetUV'
    result = core.bake_image(bpy.context, request(obj, mat, node))
    close(sample(result, .75, .25), sample(node.image, .25, .25))
    bpy.data.images.remove(result)


def mapping_chain():
    obj, mat, node = setup()
    tree = mat.node_tree
    uv = node.inputs['Vector'].links[0].from_node
    mapping = tree.nodes.new('ShaderNodeMapping')
    mapping.inputs['Location'].default_value[0] = .5
    tree.links.new(uv.outputs['UV'], mapping.inputs['Vector'])
    tree.links.new(mapping.outputs['Vector'], node.inputs['Vector'])
    assert core.infer_input_uv(mat, [], node, obj.data)[0] == 'SourceUV'
    result = core.bake_image(bpy.context, request(obj, mat, node))
    close(sample(result, .75, .25), sample(node.image, .75, .25))
    bpy.data.images.remove(result)
    result = core.bake_image(bpy.context, request(obj, mat, node, keep_mapping=False))
    close(sample(result, .75, .25), sample(node.image, .25, .25))
    bpy.data.images.remove(result)


def nested_group():
    obj, mat, original = setup()
    root = mat.node_tree
    outer_tree = bpy.data.node_groups.new('Outer', 'ShaderNodeTree')
    outer = root.nodes.new('ShaderNodeGroup')
    outer.node_tree = outer_tree
    inner_tree = bpy.data.node_groups.new('Inner', 'ShaderNodeTree')
    inner_tree.interface.new_socket(name='UV', in_out='INPUT', socket_type='NodeSocketVector')
    inner = outer_tree.nodes.new('ShaderNodeGroup')
    inner.node_tree = inner_tree
    node = inner_tree.nodes.new('ShaderNodeTexImage')
    node.image = original.image
    node.interpolation = 'Closest'
    group_in = inner_tree.nodes.new('NodeGroupInput')
    inner_tree.links.new(group_in.outputs['UV'], node.inputs['Vector'])
    uv = outer_tree.nodes.new('ShaderNodeUVMap')
    uv.uv_map = 'SourceUV'
    outer_tree.links.new(uv.outputs['UV'], inner.inputs['UV'])
    names = (outer.name, inner.name)
    assert core.infer_input_uv(mat, [outer, inner], node, obj.data)[0] == 'SourceUV'
    state = counts()
    result = core.bake_image(bpy.context, request(obj, mat, node, names))
    close(sample(result, .75, .25), sample(original.image, .25, .25))
    assert len(inner_tree.interface.items_tree) == 1
    assert len(outer_tree.interface.items_tree) == 0
    bpy.data.images.remove(result)
    assert counts() == state


def failure_cleanup():
    obj, mat, node = setup()
    state = counts()
    req = request(obj, mat, node)
    req.output_uv = 'Missing'
    try:
        core.bake_image(bpy.context, req)
        raise AssertionError('Expected missing UV error')
    except core.BakeError:
        pass
    assert counts() == state
    coords = mat.node_tree.nodes.new('ShaderNodeTexCoord')
    mat.node_tree.links.new(coords.outputs['Generated'], node.inputs['Vector'])
    try:
        core.bake_image(bpy.context, request(obj, mat, node))
        raise AssertionError('Expected unsupported coordinates error')
    except core.BakeError:
        pass
    assert counts() == state
    result = core.bake_image(bpy.context, request(obj, mat, node, keep_mapping=False))
    bpy.data.images.remove(result)
    assert counts() == state


def result_nodes_and_collision():
    obj, mat, node = setup()
    tree = mat.node_tree
    frame = tree.nodes.new('NodeFrame')
    node.parent = frame
    image = core.bake_image(bpy.context, request(obj, mat, node))
    first = core.save_unique_png(image, OUT, 'collision')
    contents = first.read_bytes()
    second = core.save_unique_png(image, OUT, 'collision')
    assert first != second and first.read_bytes() == contents
    count = len(tree.nodes)
    output = core.add_result_nodes(tree, node, image, 'TargetUV')
    assert len(tree.nodes) == count + 2
    assert output.parent == frame
    assert output.inputs['Vector'].links[0].from_node.uv_map == 'TargetUV'
    assert not output.outputs['Color'].links
    assert output.image == image
    assert output.location.x > node.location.x


def channel_packed_and_no_alpha():
    obj, mat, node = setup()
    node.image.alpha_mode = 'CHANNEL_PACKED'
    result = core.bake_image(bpy.context, request(obj, mat, node))
    assert result.alpha_mode == 'CHANNEL_PACKED'
    close(sample(result, .75, .25), sample(node.image, .25, .25))
    bpy.data.images.remove(result)
    result = core.bake_image(bpy.context, request(obj, mat, node, bake_alpha=False))
    close(sample(result, .75, .25)[:3], sample(node.image, .25, .25)[:3])
    assert sample(result, .75, .25)[3] == 1.0
    bpy.data.images.remove(result)


def failed_node_rollback():
    from unittest.mock import patch
    obj, mat, node = setup()
    image = core.bake_image(bpy.context, request(obj, mat, node))
    tree = mat.node_tree
    active = tree.nodes.active
    before = len(tree.nodes)
    # Invalid RNA enum deliberately fails after both output nodes have been added.
    class BadSource:
        parent = None
        location = node.location
        width = node.width
        interpolation = 'INVALID'
    try:
        core.add_result_nodes(tree, BadSource(), image, 'TargetUV')
        raise AssertionError('Expected node configuration failure')
    except TypeError:
        pass
    assert len(tree.nodes) == before and tree.nodes.active == active
    bpy.data.images.remove(image)


def multi_material():
    obj, mat, node = setup()
    # Split into two halves. Only the left half uses the selected material.
    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata([(0,0,0),(.5,0,0),(1,0,0),(0,1,0),(.5,1,0),(1,1,0)], [],
                     [(0,1,4,3),(1,2,5,4)])
    for name in ('SourceUV', 'TargetUV'):
        uv = mesh.uv_layers.get(name) or mesh.uv_layers.new(name=name)
        for loop in mesh.loops:
            co = mesh.vertices[loop.vertex_index].co
            uv.data[loop.index].uv = (co.x if name == 'SourceUV' else 1-co.x, co.y)
    other = bpy.data.materials.new('Other')
    mesh.materials.append(other)
    mesh.polygons[1].material_index = 1
    result = core.bake_image(bpy.context, request(obj, mat, node))
    assert sample(result, .25, .25)[3] < .01
    close(sample(result, .75, .25), sample(node.image, .25, .25))
    bpy.data.images.remove(result)


def operator_flow():
    obj, mat, node = setup()
    addon.register()
    try:
        area = bpy.context.screen.areas[0]
        area.type = 'NODE_EDITOR'
        area.spaces.active.tree_type = 'ShaderNodeTree'
        bpy.ops.wm.save_as_mainfile(filepath=str(OUT / 'operator_fixture.blend'))
        with bpy.context.temp_override(area=area):
            assert addon.editor_source(bpy.context)[3] == node
            status = bpy.ops.node.bake_to_other_uv('EXEC_DEFAULT',
                object_name=obj.name, material_name=mat.name, node_name=node.name,
                input_uv='SourceUV', output_uv='TargetUV', width=32, height=32,
                margin=0, output_mode='IMPORT', filename='operator_result')
        assert 'FINISHED' in status
        naming = bpy.context.window_manager.operator_properties_last('node.bake_to_other_uv')
        naming.source_image_name = 'MakePaint.v7.png'
        naming.filename = ''
        naming.output_uv = 'TargetUV'
        assert naming.filename == 'MakePaint.v7_TargetUV.PNG'
        naming.output_uv = 'SourceUV'
        assert naming.filename == 'MakePaint.v7_SourceUV.PNG'
        naming.filename = '手动命名.PNG'
        naming.output_uv = 'TargetUV'
        assert naming.filename == '手动命名.PNG'
        assert addon.default_output_name('Face.v2', 'UV.001') == 'Face.v2_UV.001.PNG'
        output = mat.node_tree.nodes.active
        assert output != node and output.type == 'TEX_IMAGE'
        assert output.image.filepath.startswith('//TEX/') or output.image.filepath.startswith('//TEX\\')
        assert Path(bpy.path.abspath(output.image.filepath)).exists()
        assert output.image.source == 'FILE'
        assert output.image.name.endswith('.PNG')
        close(sample(output.image, .75, .25), sample(node.image, .25, .25))
        output.image.reload()
        close(sample(output.image, .75, .25), sample(node.image, .25, .25))
        state = counts()
        with bpy.context.temp_override(area=area):
            status = bpy.ops.node.bake_to_other_uv('EXEC_DEFAULT',
                object_name=obj.name, material_name=mat.name, node_name=node.name,
                input_uv='SourceUV', output_uv='TargetUV', width=32, height=32,
                margin=0, output_mode='PNG', directory=str(OUT), filename='export_operator')
        assert 'FINISHED' in status
        assert counts() == state
    finally:
        addon.unregister()


def context_menu_invokes_dialog():
    from types import SimpleNamespace

    obj, mat, node = setup()
    area = bpy.context.screen.areas[0]
    area.type = 'NODE_EDITOR'
    area.spaces.active.tree_type = 'ShaderNodeTree'
    called = []

    class MenuLayout:
        def __init__(self, operator_context='EXEC_REGION_WIN'):
            self.operator_context = operator_context

        def separator(self):
            pass

        def row(self):
            return MenuLayout(self.operator_context)

        def operator(self, identifier, **kwargs):
            called.append((identifier, self.operator_context))

    layout = MenuLayout()
    state = counts()
    with bpy.context.temp_override(area=area):
        addon.draw_context_menu(SimpleNamespace(layout=layout), bpy.context)
        # Reproduce Blender's real menu default; EXEC skips invoke and caused the bug.
        assert called == [('node.bake_to_other_uv', 'INVOKE_DEFAULT')], called
        assert layout.operator_context == 'EXEC_REGION_WIN'
        captured = []

        class DialogManager:
            def invoke_props_dialog(self, operator, **kwargs):
                captured.append(operator)
                return {'RUNNING_MODAL'}

        fake_context = SimpleNamespace(space_data=bpy.context.space_data,
            object=obj, window_manager=DialogManager())
        operator = SimpleNamespace()
        status = addon.NODE_OT_bake_to_other_uv.invoke(operator, fake_context, None)
        assert status == {'RUNNING_MODAL'} and captured == [operator]
        assert (operator.object_name, operator.material_name, operator.node_name) == (
            obj.name, mat.name, node.name)
        assert operator.input_uv == 'SourceUV' and operator.output_uv == 'TargetUV'
        assert (operator.width, operator.height) == tuple(node.image.size)
        assert operator.group_path == '[]'
        assert operator.filename == 'Input_TargetUV.PNG'
        assert counts() == state


try:
    check('Non-Color UV mirror, alpha, PNG roundtrip and scene preservation', lambda: pixels_and_png('Non-Color'))
    check('sRGB UV mirror, alpha and PNG roundtrip', lambda: pixels_and_png('sRGB'))
    check('Explicit and implicit input/default output UV', uv_defaults)
    check('Mapping retained and bypassed', mapping_chain)
    check('Nested groups and group input coordinate tracing', nested_group)
    check('Failed bake cleanup and coordinate fallback', failure_cleanup)
    check('Adjacent nodes, frame parent and collision handling', result_nodes_and_collision)
    check('Channel-packed data and alpha-disabled export', channel_packed_and_no_alpha)
    check('Partial output node rollback', failed_node_rollback)
    check('Only selected material faces are baked', multi_material)
    check('Right-click EXEC menu invokes dialog and initializes source and UV defaults', context_menu_invokes_dialog)
    check('Registered operator import and export flows', operator_flow)
    print('ALL PASSED:', len(PASSED), 'Blender', bpy.app.version_string, flush=True)
except Exception:
    traceback.print_exc()
    print('FAILED after', len(PASSED), 'checks', flush=True)
    sys.exit(1)
