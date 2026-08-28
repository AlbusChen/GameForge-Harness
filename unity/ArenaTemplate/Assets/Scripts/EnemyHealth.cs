using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class EnemyHealth : MonoBehaviour
    {
        [SerializeField] private string entityId = string.Empty;
        [SerializeField] private int maxHealth = 20;

        public string EntityId => entityId;
        public int CurrentHealth { get; private set; }
        public string State { get; set; } = "spawning";

        private void Awake()
        {
            CurrentHealth = maxHealth;
        }

        private void Start()
        {
            GameController.Instance.RegisterEnemy(this);
            State = "chasing";
        }

        public void Configure(string id, int health)
        {
            entityId = id;
            maxHealth = Mathf.Max(1, health);
            CurrentHealth = maxHealth;
        }

        public void TakeDamage(int amount)
        {
            if (amount <= 0 || CurrentHealth <= 0)
            {
                return;
            }

            CurrentHealth = Mathf.Max(0, CurrentHealth - amount);
            if (CurrentHealth == 0)
            {
                State = "dead";
                GameController.Instance.NotifyEnemyDied(this);
                Destroy(gameObject);
            }
        }
    }
}
