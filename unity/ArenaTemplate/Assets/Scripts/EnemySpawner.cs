using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class EnemySpawner : MonoBehaviour
    {
        private static readonly Vector3[] SpawnPoints =
        {
            new Vector3(-6f, 1f, 6f),
            new Vector3(0f, 1f, 7f),
            new Vector3(6f, 1f, 6f),
            new Vector3(-7f, 1f, -5f),
            new Vector3(7f, 1f, -5f),
        };

        [SerializeField] private int count = 5;
        [SerializeField] private int health = 20;
        [SerializeField] private int damage = 10;
        [SerializeField] private float speed = 3f;

        public void Configure(int enemyCount, int enemyHealth, int enemyDamage, float enemySpeed)
        {
            count = Mathf.Clamp(enemyCount, 1, SpawnPoints.Length);
            health = Mathf.Max(1, enemyHealth);
            damage = Mathf.Max(1, enemyDamage);
            speed = Mathf.Max(0f, enemySpeed);
        }

        public void SpawnAll()
        {
            for (int index = 0; index < count; index++)
            {
                CreateEnemy(index, SpawnPoints[index]);
            }
        }

        private void CreateEnemy(int index, Vector3 position)
        {
            GameObject enemy = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            enemy.name = $"Enemy-{index + 1:000}";
            enemy.transform.position = position;

            Rigidbody body = enemy.AddComponent<Rigidbody>();
            body.constraints = RigidbodyConstraints.FreezeRotationX | RigidbodyConstraints.FreezeRotationZ;
            body.interpolation = RigidbodyInterpolation.Interpolate;

            EnemyHealth enemyHealth = enemy.AddComponent<EnemyHealth>();
            enemyHealth.Configure($"enemy-{index + 1:000}", health);
            EnemyController controller = enemy.AddComponent<EnemyController>();
            controller.Configure(speed, damage);
        }
    }
}
