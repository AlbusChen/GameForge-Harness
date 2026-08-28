using System.Collections;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace VerifiedGameBuilder.Game.Tests
{
    public sealed class ArenaAcceptanceTests
    {
        private TestCommandReceiver receiver;

        [UnitySetUp]
        public IEnumerator LoadArena()
        {
            SceneManager.LoadScene("Arena");
            yield return null;
            yield return null;
            receiver = Object.FindFirstObjectByType<TestCommandReceiver>();
            Assert.That(receiver, Is.Not.Null);
            Assert.That(GameController.Instance.EnemiesAlive, Is.EqualTo(5));
        }

        [UnityTest]
        public IEnumerator PlayerMovesAtLeastThreeMetersInOneSecond()
        {
            Vector3 start = Object.FindFirstObjectByType<PlayerController>().transform.position;
            yield return receiver.Move(Vector3.forward, 1f);
            Vector3 end = Object.FindFirstObjectByType<PlayerController>().transform.position;
            Assert.That(Vector3.Distance(start, end), Is.GreaterThanOrEqualTo(3f));
        }

        [UnityTest]
        public IEnumerator FireDamagesEnemyByTen()
        {
            EnemyHealth enemy = GameController.Instance.Enemies[0];
            enemy.GetComponent<EnemyController>().enabled = false;
            PlayerController player = Object.FindFirstObjectByType<PlayerController>();
            enemy.transform.position = player.transform.position + Vector3.forward * 3f;
            int before = enemy.CurrentHealth;

            Assert.That(receiver.AimAt(enemy.EntityId), Is.True);
            Assert.That(receiver.Fire(), Is.True);
            yield return new WaitForSeconds(0.5f);

            Assert.That(enemy.CurrentHealth, Is.EqualTo(before - 10));
        }

        [UnityTest]
        public IEnumerator EliminatingEnemiesWinsAndRestartResetsState()
        {
            receiver.EliminateAllEnemies();
            yield return null;
            Assert.That(GameController.Instance.Status, Is.EqualTo(GameStatus.Won));
            Assert.That(GameController.Instance.EnemiesAlive, Is.Zero);

            receiver.Restart();
            yield return null;
            yield return null;
            PlayerHealth health = Object.FindFirstObjectByType<PlayerHealth>();
            Assert.That(GameController.Instance.Status, Is.EqualTo(GameStatus.Playing));
            Assert.That(health.CurrentHealth, Is.EqualTo(100));
            Assert.That(GameController.Instance.EnemiesAlive, Is.EqualTo(5));
        }

        [UnityTest]
        public IEnumerator ZeroHealthLosesAndProbeReturnsState()
        {
            receiver.DamagePlayer(100);
            yield return null;
            Assert.That(GameController.Instance.Status, Is.EqualTo(GameStatus.Lost));

            string snapshot = receiver.GetSnapshot();
            StringAssert.Contains("\"gameStatus\": \"lost\"", snapshot);
            StringAssert.Contains("\"health\": 0", snapshot);
            StringAssert.Contains("\"enemiesAlive\": 5", snapshot);
            StringAssert.Contains("\"currentWave\": 1", snapshot);
        }
    }
}
