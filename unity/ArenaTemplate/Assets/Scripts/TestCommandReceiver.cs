using System.Collections;
using System.Linq;
using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class TestCommandReceiver : MonoBehaviour
    {
        private PlayerController playerController;
        private PlayerHealth playerHealth;
        private WeaponController weapon;
        private RuntimeStateProbe stateProbe;

        private void Awake()
        {
            playerController = FindFirstObjectByType<PlayerController>();
            playerHealth = playerController.GetComponent<PlayerHealth>();
            weapon = playerController.GetComponent<WeaponController>();
            stateProbe = FindFirstObjectByType<RuntimeStateProbe>();
        }

        public IEnumerator Move(Vector3 direction, float duration)
        {
            yield return playerController.MoveForTest(direction, duration);
        }

        public bool AimAt(string entityId)
        {
            EnemyHealth enemy = GameController.Instance.Enemies.FirstOrDefault(
                candidate => candidate != null && candidate.EntityId == entityId);
            if (enemy == null)
            {
                return false;
            }

            weapon.SetTestAim(enemy.transform.position);
            return true;
        }

        public bool Fire()
        {
            return weapon.FireForTest();
        }

        public bool Damage(string entityId, int amount)
        {
            EnemyHealth enemy = GameController.Instance.Enemies.FirstOrDefault(
                candidate => candidate != null && candidate.EntityId == entityId);
            if (enemy == null)
            {
                return false;
            }

            enemy.TakeDamage(amount);
            return true;
        }

        public void DamagePlayer(int amount)
        {
            playerHealth.TakeDamage(amount);
        }

        public void EliminateAllEnemies()
        {
            EnemyHealth[] enemies = GameController.Instance.Enemies.Where(item => item != null).ToArray();
            foreach (EnemyHealth enemy in enemies)
            {
                enemy.TakeDamage(enemy.CurrentHealth);
            }
        }

        public void Restart()
        {
            GameController.Instance.Restart();
        }

        public string GetSnapshot()
        {
            return stateProbe.CaptureJson();
        }
    }
}
