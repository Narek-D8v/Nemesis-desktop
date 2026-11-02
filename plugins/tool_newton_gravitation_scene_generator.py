from nemus.core.tools.base import Tool, ToolResult
import json


class NewtonGravitationSceneGenerator(Tool):
    name = "newton_gravitation_scene_generator"
    description = "Generates JSON scene graph for hyperframes video about Newton's law of universal gravitation with cinematic style and 4 scenes"

    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, **_):
        scene_graph = {
            "project_info": {
                "title": "Закон всемирного тяготения: Озарение Исаака Ньютона",
                "resolution": "1920x1080",
                "fps": 30,
                "aspect_ratio": "16:9",
                "style": "kinematograficheskiy realizm XVII vek palitra, teplaya zakatnaya podslushka, soft shadows, depth of field",
                "transitions": "shader-transitions pлавnye bez ryvkov",
            },
            "scenes": [
                {
                    "id": "scene_1",
                    "duration_sec": 4,
                    "title": "Сад в Вулсторпе",
                    "visual_row": "English garden XVII century. Isaac Newton in historical clothing sits thoughtfully under a huge spreading apple tree. On knees — parchment with drawings.",
                    "camera": "Slow zoom-in on scientist's face",
                    "audio_mood": "calm, orchestral",
                },
                {
                    "id": "scene_2",
                    "duration_sec": 3,
                    "title": "Ветка и падение",
                    "visual_row": "Macro shot on tree branch. Ripe red apple detaches and falls downward. Cinematic slow motion.",
                    "camera": "Dynamic following of falling apple",
                    "effects": "slow_motion, depth_of_field",
                },
                {
                    "id": "scene_3",
                    "duration_sec": 5,
                    "title": "Момент «Эврика!»",
                    "visual_row": "Apple lightly touches Newton's head. He sharply lifts his head upward, sudden enlightenment on his face. Around him in air for a fraction of second elegantly appear glowing gravitational vector lines and formulas.",
                    "camera": "Smooth orbital flight around hero",
                    "effects": "glowing_lines, formular_appearance, depth_of_field",
                },
                {
                    "id": "scene_4",
                    "duration_sec": 3,
                    "title": "Заключение",
                    "visual_row": "Wide shot of garden against sunset background. On screen appears minimalist typography with quote or theme of the video.",
                    "camera": "Wide static shot",
                    "typography": "Minimalist text with citation about gravity",
                },
            ],
            "technical_specs": {
                "codec": "h.264",
                "bitrate": "10Mbps",
                "audio": "48kHz stereo",
                "render_engine": "hyperframes local engine",
                "packages_required": ["cli", "producer", "engine", "shader-transitions"],
            },
        }
        json_output = json.dumps(scene_graph, ensure_ascii=False, indent=2)
        return ToolResult.success(json_output)


# Instantiate the generator
generator = NewtonGravitationSceneGenerator()
result = generator.execute()
print(
    result.data
    if hasattr(result, "data")
    else result.text
    if hasattr(result, "text")
    else str(result)
)
