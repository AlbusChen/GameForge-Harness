using NUnit.Framework;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace VerifiedGameBuilder.Game.Tests
{
    public sealed class ArenaStructureTests
    {
        [SetUp]
        public void OpenArena()
        {
            EditorSceneManager.OpenScene("Assets/Scenes/Arena.unity");
        }

        [Test]
        public void SceneContainsRequiredSystems()
        {
            Assert.That(Object.FindFirstObjectByType<GameController>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<PlayerController>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<PlayerHealth>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<WeaponController>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<EnemySpawner>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<HudController>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<RuntimeStateProbe>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<TestCommandReceiver>(), Is.Not.Null);
            Assert.That(Object.FindFirstObjectByType<BuildSmokeProbe>(), Is.Not.Null);
        }

        [Test]
        public void MainCameraAndArenaBoundariesExist()
        {
            Assert.That(Camera.main, Is.Not.Null);
            Assert.That(GameObject.Find("Arena Floor"), Is.Not.Null);
            Assert.That(GameObject.Find("North Wall"), Is.Not.Null);
            Assert.That(GameObject.Find("South Wall"), Is.Not.Null);
            Assert.That(GameObject.Find("East Wall"), Is.Not.Null);
            Assert.That(GameObject.Find("West Wall"), Is.Not.Null);
        }
    }
}
