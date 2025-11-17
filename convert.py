import bpy
import os
import shutil
import xml.etree.ElementTree as ET
import re
from xml.dom import minidom
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty
from bpy.types import Operator

# Target blender version: 4.x (compatible with 2.82+)

########################################################################################################################
### Exports model.dae of the scene with textures, its corresponding model.sdf file, and a default model.config file ####
### Now also injects per-visual collisions with material-based surface parameters on first run.                    ####
########################################################################################################################

# --- Collision material presets ---
MATERIALS = {
    "wood": {"mu": 0.5, "mu2": 0.5, "restitution": 0.2},
    "concrete": {"mu": 1.0, "mu2": 1.0, "restitution": 0.05},
    "glass": {"mu": 0.4, "mu2": 0.4, "restitution": 0.03},
    "default": {"mu": 0.8, "mu2": 0.8, "restitution": 0.05},
}

# --- Heuristic category detection (EN + VI common keywords) ---
WOOD_KEYS = [
    # EN
    "tree", "forest", "trunk", "branch", "wood", "bark", "plant",
    # VI
    "go", "gỗ", "cay", "cây", "than", "thân", "canh", "cành",
]
GLASS_KEYS = [
    # EN
    "glass", "window", "glazing",
    # VI
    "kinh", "kính", "cua_kinh", "cửa kính", "mat_kinh", "mặt kính", "vach_kinh", "vách kính",
]
ROAD_KEYS = [
    # EN
    "road", "street", "avenue", "lane", "alley", "highway", "junction", "intersection",
    "crossing", "crosswalk", "roundabout", "parking", "parking_lot", "surfacearea", "roadarea",
    "pavement", "sidewalk", "walkway", "driveway", "kerb", "curb", "asphalt", "concrete",
    # VI
    "duong", "đường", "pho", "phố", "ngo", "ngõ", "ngach", "ngách", "dai_lo", "đại lộ",
    "vach", "vạch", "bai_do", "bãi đỗ", "via_he", "vỉa hè", "loi_di", "lối đi", "be_tong", "bê tông",
    "nhua", "nhựa",
]


def _sanitize_name(name: str) -> str:
    s = name.strip()
    s = re.sub(r"[^0-9A-Za-z_]+", "_", s)
    if s and s[0].isdigit():
        s = f"n_{s}"
    return s


def _pick_material(name: str) -> str:
    n_norm = name.lower().replace(" ", "_")
    if any(k in n_norm for k in WOOD_KEYS):
        return "wood"
    if any(k in n_norm for k in GLASS_KEYS):
        return "glass"
    if any(k in n_norm for k in ROAD_KEYS):
        return "concrete"
    return "default"


def _add_collision_for_visual(link_el: ET.Element, visual_el: ET.Element, existing_names: set, meshes_folder_prefix: str, dae_filename: str):
    """Create a collision sibling for given visual, copying its geometry and adding surface params."""
    vname = visual_el.get("name", "visual")
    col_name = _sanitize_name(f"col_{vname}")
    if col_name in existing_names:
        return False

    geom = visual_el.find("geometry")
    if geom is None:
        # Visual without geometry shouldn't happen here, skip
        return False

    # Build <collision>
    collision = ET.Element("collision", attrib={"name": col_name})

    # Copy geometry structure to collision
    # For robustness, rebuild minimal geometry from known export pattern
    mesh = geom.find("mesh")
    if mesh is not None:
        # Deep copy the existing mesh node
        collision_geom = ET.SubElement(collision, "geometry")
        collision_mesh = ET.SubElement(collision_geom, "mesh")
        # uri
        uri_el = mesh.find("uri")
        uri_text = uri_el.text if uri_el is not None else meshes_folder_prefix + dae_filename
        uri_copy = ET.SubElement(collision_mesh, "uri")
        uri_copy.text = uri_text
        # submesh
        submesh = mesh.find("submesh")
        if submesh is not None:
            submesh_copy = ET.SubElement(collision_mesh, "submesh")
            name_node = submesh.find("name")
            name_text = name_node.text if name_node is not None else vname
            name_copy = ET.SubElement(submesh_copy, "name")
            name_copy.text = name_text
    else:
        # Fallback: copy whole geometry
        collision.append(ET.fromstring(ET.tostring(geom)))

    # Surface parameters based on heuristic
    mkey = _pick_material(vname)
    preset = MATERIALS[mkey]
    surface = ET.SubElement(collision, "surface")
    friction = ET.SubElement(surface, "friction")
    ode = ET.SubElement(friction, "ode")
    ET.SubElement(ode, "mu").text = str(preset["mu"])
    ET.SubElement(ode, "mu2").text = str(preset["mu2"])
    if mkey == "glass":
        ET.SubElement(ode, "slip1").text = "0.02"
        ET.SubElement(ode, "slip2").text = "0.02"
    bounce = ET.SubElement(surface, "bounce")
    ET.SubElement(bounce, "restitution_coefficient").text = str(preset["restitution"])
    ET.SubElement(bounce, "threshold").text = "100.0"
    contact = ET.SubElement(surface, "contact")
    ode_c = ET.SubElement(contact, "ode")
    ET.SubElement(ode_c, "kp").text = "1e6"
    ET.SubElement(ode_c, "kd").text = "1.0"

    # Insert right after the visual element for readability
    idx = list(link_el).index(visual_el) + 1
    link_el.insert(idx, collision)
    existing_names.add(col_name)
    return True
def export_sdf(prefix_path):
    dae_filename = 'model.dae'  # Giữ nguyên theo yêu cầu
    sdf_filename = 'model.sdf'  # Giữ nguyên theo yêu cầu
    model_config_filename = 'model.config'  # Giữ nguyên theo yêu cầu
    lightmap_filename = 'LightmapBaked.png'  # Có thể tùy biến, nhưng giữ mặc định
    model_name = 'my_model'  # Giữ nguyên theo yêu cầu
    meshes_folder_prefix = 'meshes/'  # Tùy biến: Tạo thư mục nếu chưa tồn tại

    # Tạo thư mục meshes nếu chưa tồn tại
    meshes_path = os.path.join(prefix_path, meshes_folder_prefix)
    os.makedirs(meshes_path, exist_ok=True)

    # Thu thập và sao chép texture từ Principled BSDF
    texture_files = set()  # Lưu danh sách texture để sao chép
    for obj in bpy.context.selectable_objects:
        if obj.type == 'MESH' and obj.active_material and obj.active_material.node_tree:
            nodes = obj.active_material.node_tree.nodes
            principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if principled and principled.inputs.get('Base Color') and principled.inputs['Base Color'].links:
                link_node = principled.inputs['Base Color'].links[0].from_node
                if hasattr(link_node, 'image') and link_node.image:
                    texture_path = bpy.path.abspath(link_node.image.filepath)
                    if os.path.isfile(texture_path):
                        texture_files.add(texture_path)
                    else:
                        print(f"Texture path không hợp lệ: {texture_path}")

    # Sao chép texture vào thư mục meshes
    for texture_path in texture_files:
        try:
            destination = os.path.join(meshes_path, os.path.basename(texture_path))
            shutil.copy2(texture_path, destination)
            print(f"Đã sao chép texture: {os.path.basename(texture_path)} vào {meshes_path}")
        except Exception as e:
            print(f"Lỗi khi sao chép texture {texture_path}: {e}")

    # Export DAE với tối ưu hóa
    try:
        bpy.ops.wm.collada_export(
            filepath=os.path.join(meshes_path, dae_filename),
            check_existing=False,
            apply_modifiers=True,
            triangulate=True,
            include_normals=True,           # ← BẬT NORMALS (quan trọng nhất!)
            include_uvs=True,
            include_material_textures=True,
            use_mesh_modifiers=True,
            sort_by_name=True,
            open_sim=True,                  # ← Tối ưu cho Gazebo/Ignition
            # Các tham số SAI đã bị xóa:
            # export_selected_only=False,     ← XÓA
            # selected_objects_only=False,    ← XÓA
            # Các filter giữ nguyên
            filter_blender=False,
            filter_image=False,
            filter_movie=False,
            filter_python=False,
            filter_font=False,
            filter_sound=False,
            filter_text=False,
            filter_btx=False,
            filter_collada=True,
            filter_folder=True,
            filemode=8
        )
        print(f"Đã xuất DAE thành công vào: {os.path.join(meshes_path, dae_filename)}")
    except Exception as e:
        print(f"Lỗi khi xuất DAE: {e}")
        return

    objects = bpy.context.selectable_objects
    mesh_objects = [o for o in objects if o.type == 'MESH']

    #############################################
    #### Xuất SDF xml dựa trên scene ############
    #############################################
    sdf = ET.Element('sdf', attrib={'version': '1.8'})

    # 1 model và 1 link
    model = ET.SubElement(sdf, "model", attrib={"name": model_name})
    
    # Static mặc định true (giữ tĩnh theo yêu cầu)
    static = ET.SubElement(model, "static")
    static.text = "true"

    link = ET.SubElement(model, "link", attrib={"name": "testlink"})

    # Thêm <visual> cho mỗi mesh và tự động thêm <collision> tương ứng
    existing_collision_names = set()
    for o in mesh_objects:
        visual = ET.SubElement(link, "visual", attrib={"name": o.name})

        geometry = ET.SubElement(visual, "geometry")
        mesh = ET.SubElement(geometry, "mesh")
        uri = ET.SubElement(mesh, "uri")
        uri.text = meshes_folder_prefix + dae_filename
        submesh = ET.SubElement(mesh, "submesh")
        submesh_name = ET.SubElement(submesh, "name")
        submesh_name.text = o.name
        
        # Kiểm tra và thêm material chỉ khi có texture
        diffuse_map = ""
        metal_node = None
        if o.active_material and o.active_material.node_tree:
            nodes = o.active_material.node_tree.nodes
            principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if principled:
                base_color = principled.inputs.get('Base Color')
                if base_color and base_color.links:
                    link_node = base_color.links[0].from_node
                    if hasattr(link_node, 'image') and link_node.image:
                        texture_path = bpy.path.abspath(link_node.image.filepath)
                        if os.path.isfile(os.path.join(meshes_path, os.path.basename(texture_path))):
                            diffuse_map = os.path.basename(texture_path)
                        else:
                            print(f"Texture {texture_path} không tìm thấy trong {meshes_path}")

        # Thêm material chỉ khi có texture
        if diffuse_map:
            material = ET.SubElement(visual, "material")
            diffuse = ET.SubElement(material, "diffuse")
            diffuse.text = "1.0 1.0 1.0 1.0"  # Giữ sáng
            specular = ET.SubElement(material, "specular")
            specular.text = "0.0 0.0 0.0 1.0"
            pbr = ET.SubElement(material, "pbr")
            metal_node = ET.SubElement(pbr, "metal")
            albedo_map = ET.SubElement(metal_node, "albedo_map")
            albedo_map.text = meshes_folder_prefix + diffuse_map
        
        # Kiểm tra lightmap (tùy chọn)
        lightmap_full_path = os.path.join(meshes_path, lightmap_filename)
        if os.path.isfile(lightmap_full_path):
            # Đảm bảo có pbr/metal để đặt light_map, nếu chưa có thì tạo tối thiểu
            if metal_node is None:
                material = visual.find("material")
                if material is None:
                    material = ET.SubElement(visual, "material")
                pbr = material.find("pbr")
                if pbr is None:
                    pbr = ET.SubElement(material, "pbr")
                metal_node = ET.SubElement(pbr, "metal")
            light_map = ET.SubElement(metal_node, "light_map", attrib={"uv_set": "1"})  # UV set 1
            light_map.text = meshes_folder_prefix + lightmap_filename
            cast_shadows = ET.SubElement(visual, "cast_shadows")
            cast_shadows.text = "0"  # Tắt bóng
        else:
            print(f"Lightmap {lightmap_filename} không tìm thấy trong {meshes_path}")

        # Tự động thêm collision dựa trên visual vừa tạo
        _add_collision_for_visual(link, visual, existing_collision_names, meshes_folder_prefix, dae_filename)

    # Không thêm <light> (đã thêm <collision> tự động ở trên)

    # Ghi SDF vào file
    try:
        xml_string = ET.tostring(sdf, encoding='unicode')
        reparsed = minidom.parseString(xml_string)
        with open(os.path.join(prefix_path, sdf_filename), "w") as sdf_file:
            sdf_file.write(reparsed.toprettyxml(indent="  "))
    except Exception as e:
        print(f"Lỗi khi ghi SDF: {e}")
        return

    # Tạo model.config
    config_model = ET.Element('model')
    name_elem = ET.SubElement(config_model, 'name')
    name_elem.text = model_name
    version = ET.SubElement(config_model, 'version')
    version.text = "1.0"
    sdf_tag = ET.SubElement(config_model, "sdf", attrib={"version": "1.8"})
    sdf_tag.text = sdf_filename
    author = ET.SubElement(config_model, 'author')
    author_name = ET.SubElement(author, 'name')
    author_name.text = "Generated by blender SDF tools"

    try:
        xml_string = ET.tostring(config_model, encoding='unicode')
        reparsed = minidom.parseString(xml_string)
        with open(os.path.join(prefix_path, model_config_filename), "w") as config_file:
            config_file.write(reparsed.toprettyxml(indent="  "))
    except Exception as e:
        print(f"Lỗi khi ghi model.config: {e}")
        return

    print("Xuất thành công: model.dae, model.sdf, model.config")

#### UI Handling ####
class OT_TestOpenFilebrowser(Operator, ImportHelper):
    bl_idname = "test.open_filebrowser"
    bl_label = "Save SDF Model"
  
    directory: StringProperty(name="Outdir Path", description="Thư mục đầu ra")
  
    def execute(self, context):
        if not os.path.isdir(self.directory):
            self.report({'ERROR'}, f"{self.directory} không phải là thư mục hợp lệ!")
            return {'CANCELLED'}
        print(f"Xuất sang thư mục: {self.directory}")
        export_sdf(self.directory)
        self.report({'INFO'}, "Xuất SDF thành công!")
        return {'FINISHED'}

def register(): 
    bpy.utils.register_class(OT_TestOpenFilebrowser) 
def unregister(): 
    bpy.utils.unregister_class(OT_TestOpenFilebrowser)
    
if __name__ == "__main__":
    register() 
    bpy.ops.test.open_filebrowser('INVOKE_DEFAULT')
