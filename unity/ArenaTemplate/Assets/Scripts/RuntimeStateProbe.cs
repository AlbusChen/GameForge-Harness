using System;
using VerifiedGameBuilder.AgentBridge;
using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class RuntimeStateProbe : MonoBehaviour
    {
        public RuntimeSnapshot Capture()
        {
            GameController gameController = GameController.Instance;
            PlayerHealth health = FindFirstObjectByType<PlayerHealth>();
            WeaponController weapon = health.GetComponent<WeaponController>();
            HudController hud = FindFirstObjectByType<HudController>();

            RuntimeSnapshot snapshot = new RuntimeSnapshot
            {
                timestamp = DateTime.UtcNow.ToString("O"),
                frame = Time.frameCount,
                gameStatus = gameController.Status.ToString().ToLowerInvariant(),
                enemiesAlive = gameController.EnemiesAlive,
                currentWave = 1,
                player = new PlayerSnapshot
                {
                    health = health.CurrentHealth,
                    maxHealth = health.MaxHealth,
                    position = health.transform.position,
                    shotsFired = weapon.ShotsFired,
                },
                ui = new UiSnapshot
                {
                    endScreenVisible = hud.EndScreenVisible,
                    healthText = hud.HealthText,
                },
            };

            foreach (EnemyHealth enemy in gameController.Enemies)
            {
                if (enemy == null)
                {
                    continue;
                }

                snapshot.enemies.Add(new EnemySnapshot
                {
                    id = enemy.EntityId,
                    health = enemy.CurrentHealth,
                    position = enemy.transform.position,
                    state = enemy.State,
                });
            }

            return snapshot;
        }

        public string CaptureJson()
        {
            return JsonUtility.ToJson(Capture(), true);
        }
    }
}
