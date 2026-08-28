using System;
using System.Collections.Generic;
using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class GameController : MonoBehaviour
    {
        private readonly List<EnemyHealth> enemies = new List<EnemyHealth>();
        private PlayerHealth playerHealth;
        private EnemySpawner enemySpawner;

        public static GameController Instance { get; private set; }
        public GameStatus Status { get; private set; } = GameStatus.Playing;
        public int EnemiesAlive => enemies.Count;
        public IReadOnlyList<EnemyHealth> Enemies => enemies;

        public event Action StateChanged;

        private void Awake()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }

            Instance = this;
            playerHealth = FindFirstObjectByType<PlayerHealth>();
            enemySpawner = FindFirstObjectByType<EnemySpawner>();
        }

        private void Start()
        {
            enemySpawner.SpawnAll();
            StateChanged?.Invoke();
        }

        private void OnDestroy()
        {
            if (Instance == this)
            {
                Instance = null;
            }
        }

        public void RegisterEnemy(EnemyHealth enemy)
        {
            if (!enemies.Contains(enemy))
            {
                enemies.Add(enemy);
                StateChanged?.Invoke();
            }
        }

        public void NotifyEnemyDied(EnemyHealth enemy)
        {
            enemies.Remove(enemy);
            if (Status == GameStatus.Playing && enemies.Count == 0)
            {
                Status = GameStatus.Won;
            }

            StateChanged?.Invoke();
        }

        public void NotifyPlayerDied()
        {
            if (Status != GameStatus.Playing)
            {
                return;
            }

            Status = GameStatus.Lost;
            StateChanged?.Invoke();
        }

        public void Restart()
        {
            for (int index = enemies.Count - 1; index >= 0; index--)
            {
                if (enemies[index] != null)
                {
                    Destroy(enemies[index].gameObject);
                }
            }

            enemies.Clear();
            Status = GameStatus.Playing;
            playerHealth.ResetHealth();
            playerHealth.transform.position = new Vector3(0f, 1f, 0f);
            enemySpawner.SpawnAll();
            StateChanged?.Invoke();
        }
    }
}
