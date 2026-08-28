using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.UI;
using VerifiedGameBuilder.Game;

namespace VerifiedGameBuilder.Editor
{
    public static class ArenaSetup
    {
        public const string ScenePath = "Assets/Scenes/Arena.unity";
        private const string GeneratedPath = "Assets/Generated";
        private const string MaterialPath = GeneratedPath + "/Materials";

        [MenuItem("GameForge/Create Arena Baseline")]
        public static void CreateArena()
        {
            EnsureFolder("Assets", "Scenes");
            EnsureFolder("Assets", "Generated");
            EnsureFolder(GeneratedPath, "Materials");

            if (AssetDatabase.LoadAssetAtPath<SceneAsset>(ScenePath) != null)
            {
                EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
                ApplyProjectSettings();
                Debug.Log($"Using existing verified arena scene at {ScenePath}");
                return;
            }

            Scene scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            CreateEnvironment();
            CreateLightingAndCamera();
            CreatePlayer();
            CreateSystems();
            CreateHud();

            EditorSceneManager.SaveScene(scene, ScenePath);
            ApplyProjectSettings();
            Debug.Log($"Created deterministic arena scene at {ScenePath}");
        }

        private static void ApplyProjectSettings()
        {
            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(ScenePath, true) };
            PlayerSettings.companyName = "AlbusChen";
            PlayerSettings.productName = "Arena Demo";
            PlayerSettings.defaultScreenWidth = 1280;
            PlayerSettings.defaultScreenHeight = 720;
            AssetDatabase.SaveAssets();
        }

        private static void CreateEnvironment()
        {
            Material floorMaterial = GetOrCreateMaterial("Floor", new Color(0.15f, 0.2f, 0.25f));
            Material wallMaterial = GetOrCreateMaterial("Wall", new Color(0.08f, 0.1f, 0.14f));

            GameObject floor = GameObject.CreatePrimitive(PrimitiveType.Cube);
            floor.name = "Arena Floor";
            floor.transform.position = new Vector3(0f, -0.25f, 0f);
            floor.transform.localScale = new Vector3(18f, 0.5f, 18f);
            floor.GetComponent<Renderer>().sharedMaterial = floorMaterial;

            CreateWall("North Wall", new Vector3(0f, 1f, 9f), new Vector3(19f, 2f, 1f), wallMaterial);
            CreateWall("South Wall", new Vector3(0f, 1f, -9f), new Vector3(19f, 2f, 1f), wallMaterial);
            CreateWall("East Wall", new Vector3(9f, 1f, 0f), new Vector3(1f, 2f, 17f), wallMaterial);
            CreateWall("West Wall", new Vector3(-9f, 1f, 0f), new Vector3(1f, 2f, 17f), wallMaterial);
        }

        private static void CreateWall(
            string name,
            Vector3 position,
            Vector3 scale,
            Material material)
        {
            GameObject wall = GameObject.CreatePrimitive(PrimitiveType.Cube);
            wall.name = name;
            wall.transform.position = position;
            wall.transform.localScale = scale;
            wall.GetComponent<Renderer>().sharedMaterial = material;
        }

        private static void CreateLightingAndCamera()
        {
            GameObject lightObject = new GameObject("Directional Light");
            Light light = lightObject.AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = 1.2f;
            lightObject.transform.rotation = Quaternion.Euler(55f, -35f, 0f);

            GameObject cameraObject = new GameObject("Main Camera");
            cameraObject.tag = "MainCamera";
            Camera camera = cameraObject.AddComponent<Camera>();
            camera.orthographic = true;
            camera.orthographicSize = 12f;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.025f, 0.035f, 0.055f);
            cameraObject.transform.position = new Vector3(0f, 18f, -8f);
            cameraObject.transform.rotation = Quaternion.Euler(66f, 0f, 0f);
        }

        private static void CreatePlayer()
        {
            GameObject player = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            player.name = "Player";
            player.tag = "Player";
            player.transform.position = new Vector3(0f, 1f, 0f);
            player.GetComponent<Renderer>().sharedMaterial =
                GetOrCreateMaterial("Player", new Color(0.15f, 0.65f, 1f));

            Rigidbody body = player.AddComponent<Rigidbody>();
            body.constraints = RigidbodyConstraints.FreezeRotationX | RigidbodyConstraints.FreezeRotationZ;
            body.interpolation = RigidbodyInterpolation.Interpolate;

            player.AddComponent<PlayerHealth>().Configure(100);
            player.AddComponent<PlayerController>().Configure(6f);
            player.AddComponent<WeaponController>().Configure(10, 4f, 20f);
        }

        private static void CreateSystems()
        {
            GameObject systems = new GameObject("Game Systems");
            systems.AddComponent<GameController>();
            systems.AddComponent<EnemySpawner>().Configure(5, 20, 10, 3f);
            systems.AddComponent<RuntimeStateProbe>();
            systems.AddComponent<TestCommandReceiver>();
            systems.AddComponent<BuildSmokeProbe>();
        }

        private static void CreateHud()
        {
            Font font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");

            GameObject canvasObject = new GameObject("HUD Canvas");
            Canvas canvas = canvasObject.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            CanvasScaler scaler = canvasObject.AddComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1280f, 720f);
            canvasObject.AddComponent<GraphicRaycaster>();

            Text health = CreateText(
                "Health Text", canvas.transform, font, new Vector2(24f, -24f), TextAnchor.UpperLeft);
            Text enemies = CreateText(
                "Enemies Text", canvas.transform, font, new Vector2(-24f, -24f), TextAnchor.UpperRight);
            RectTransform enemiesRect = enemies.rectTransform;
            enemiesRect.anchorMin = Vector2.one;
            enemiesRect.anchorMax = Vector2.one;
            enemiesRect.pivot = Vector2.one;

            GameObject panel = new GameObject("End Panel");
            panel.transform.SetParent(canvas.transform, false);
            Image panelImage = panel.AddComponent<Image>();
            panelImage.color = new Color(0.02f, 0.025f, 0.04f, 0.94f);
            RectTransform panelRect = panel.GetComponent<RectTransform>();
            panelRect.anchorMin = new Vector2(0.5f, 0.5f);
            panelRect.anchorMax = new Vector2(0.5f, 0.5f);
            panelRect.sizeDelta = new Vector2(440f, 260f);

            Text ending = CreateCenteredText("End Text", panel.transform, font, new Vector2(0f, 55f), 38);
            Button restart = CreateButton(panel.transform, font);
            panel.SetActive(false);

            HudController hud = canvasObject.AddComponent<HudController>();
            hud.Configure(health, enemies, panel, ending, restart);

            GameObject eventSystem = new GameObject("Event System");
            eventSystem.AddComponent<EventSystem>();
            eventSystem.AddComponent<StandaloneInputModule>();
        }

        private static Text CreateText(
            string name,
            Transform parent,
            Font font,
            Vector2 position,
            TextAnchor alignment)
        {
            GameObject textObject = new GameObject(name);
            textObject.transform.SetParent(parent, false);
            Text text = textObject.AddComponent<Text>();
            text.font = font;
            text.fontSize = 26;
            text.color = Color.white;
            text.alignment = alignment;
            RectTransform rect = text.rectTransform;
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(0f, 1f);
            rect.pivot = new Vector2(0f, 1f);
            rect.anchoredPosition = position;
            rect.sizeDelta = new Vector2(360f, 60f);
            return text;
        }

        private static Text CreateCenteredText(
            string name,
            Transform parent,
            Font font,
            Vector2 position,
            int fontSize)
        {
            Text text = CreateText(name, parent, font, position, TextAnchor.MiddleCenter);
            RectTransform rect = text.rectTransform;
            rect.anchorMin = new Vector2(0.5f, 0.5f);
            rect.anchorMax = new Vector2(0.5f, 0.5f);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.sizeDelta = new Vector2(380f, 80f);
            text.fontSize = fontSize;
            return text;
        }

        private static Button CreateButton(Transform parent, Font font)
        {
            GameObject buttonObject = new GameObject("Restart Button");
            buttonObject.transform.SetParent(parent, false);
            Image image = buttonObject.AddComponent<Image>();
            image.color = new Color(0.12f, 0.5f, 0.9f);
            Button button = buttonObject.AddComponent<Button>();
            button.targetGraphic = image;
            RectTransform rect = buttonObject.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0.5f, 0.5f);
            rect.anchorMax = new Vector2(0.5f, 0.5f);
            rect.anchoredPosition = new Vector2(0f, -55f);
            rect.sizeDelta = new Vector2(220f, 64f);

            Text label = CreateCenteredText("Label", buttonObject.transform, font, Vector2.zero, 24);
            label.text = "Restart";
            label.raycastTarget = false;
            label.rectTransform.anchorMin = Vector2.zero;
            label.rectTransform.anchorMax = Vector2.one;
            label.rectTransform.offsetMin = Vector2.zero;
            label.rectTransform.offsetMax = Vector2.zero;
            return button;
        }

        private static Material GetOrCreateMaterial(string name, Color color)
        {
            string path = $"{MaterialPath}/{name}.mat";
            Material material = AssetDatabase.LoadAssetAtPath<Material>(path);
            if (material == null)
            {
                Shader shader = Shader.Find("Universal Render Pipeline/Lit") ?? Shader.Find("Standard");
                material = new Material(shader) { name = name };
                AssetDatabase.CreateAsset(material, path);
            }

            material.color = color;
            EditorUtility.SetDirty(material);
            return material;
        }

        private static void EnsureFolder(string parent, string child)
        {
            string path = $"{parent}/{child}";
            if (!AssetDatabase.IsValidFolder(path))
            {
                AssetDatabase.CreateFolder(parent, child);
            }
        }
    }
}
