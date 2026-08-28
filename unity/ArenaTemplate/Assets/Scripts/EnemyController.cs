using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    [RequireComponent(typeof(Rigidbody))]
    [RequireComponent(typeof(EnemyHealth))]
    public sealed class EnemyController : MonoBehaviour
    {
        [SerializeField] private float speed = 3f;
        [SerializeField] private int contactDamage = 10;
        [SerializeField] private float attackDistance = 1.4f;
        [SerializeField] private float attackCooldown = 0.8f;

        private Rigidbody body;
        private Transform player;
        private PlayerHealth playerHealth;
        private float nextAttackTime;

        private void Awake()
        {
            body = GetComponent<Rigidbody>();
        }

        private void Start()
        {
            playerHealth = FindFirstObjectByType<PlayerHealth>();
            player = playerHealth.transform;
        }

        private void FixedUpdate()
        {
            if (GameController.Instance.Status != GameStatus.Playing || player == null)
            {
                return;
            }

            Vector3 offset = player.position - body.position;
            offset.y = 0f;
            float distance = offset.magnitude;
            if (distance > attackDistance)
            {
                Vector3 direction = offset / distance;
                body.MovePosition(body.position + direction * speed * Time.fixedDeltaTime);
                body.MoveRotation(Quaternion.LookRotation(direction, Vector3.up));
            }
            else if (Time.time >= nextAttackTime)
            {
                playerHealth.TakeDamage(contactDamage);
                nextAttackTime = Time.time + attackCooldown;
            }
        }

        public void Configure(float movementSpeed, int damage)
        {
            speed = Mathf.Max(0f, movementSpeed);
            contactDamage = Mathf.Max(1, damage);
        }
    }
}
