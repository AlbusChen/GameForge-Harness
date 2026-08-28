using System;
using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class PlayerHealth : MonoBehaviour
    {
        [SerializeField] private int maxHealth = 100;

        public int CurrentHealth { get; private set; }
        public int MaxHealth => maxHealth;
        public event Action HealthChanged;

        private void Awake()
        {
            CurrentHealth = maxHealth;
        }

        public void Configure(int value)
        {
            maxHealth = Mathf.Max(1, value);
            CurrentHealth = maxHealth;
        }

        public void TakeDamage(int amount)
        {
            if (amount <= 0 || CurrentHealth <= 0 || GameController.Instance.Status != GameStatus.Playing)
            {
                return;
            }

            CurrentHealth = Mathf.Max(0, CurrentHealth - amount);
            HealthChanged?.Invoke();
            if (CurrentHealth == 0)
            {
                GameController.Instance.NotifyPlayerDied();
            }
        }

        public void ResetHealth()
        {
            CurrentHealth = maxHealth;
            HealthChanged?.Invoke();
        }
    }
}
