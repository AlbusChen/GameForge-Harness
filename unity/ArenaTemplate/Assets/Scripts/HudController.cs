using UnityEngine;
using UnityEngine.UI;

namespace VerifiedGameBuilder.Game
{
    public sealed class HudController : MonoBehaviour
    {
        [SerializeField] private Text healthText;
        [SerializeField] private Text enemiesText;
        [SerializeField] private GameObject endPanel;
        [SerializeField] private Text endText;
        [SerializeField] private Button restartButton;

        private GameController gameController;
        private PlayerHealth playerHealth;

        public bool EndScreenVisible => endPanel != null && endPanel.activeSelf;
        public string HealthText => healthText != null ? healthText.text : string.Empty;

        public void Configure(
            Text health,
            Text enemies,
            GameObject panel,
            Text ending,
            Button restart)
        {
            healthText = health;
            enemiesText = enemies;
            endPanel = panel;
            endText = ending;
            restartButton = restart;
        }

        private void Start()
        {
            gameController = GameController.Instance;
            playerHealth = FindFirstObjectByType<PlayerHealth>();
            gameController.StateChanged += Refresh;
            playerHealth.HealthChanged += Refresh;
            restartButton.onClick.AddListener(gameController.Restart);
            Refresh();
        }

        private void OnDestroy()
        {
            if (gameController != null)
            {
                gameController.StateChanged -= Refresh;
            }

            if (playerHealth != null)
            {
                playerHealth.HealthChanged -= Refresh;
            }
        }

        private void Refresh()
        {
            healthText.text = $"Health: {playerHealth.CurrentHealth}/{playerHealth.MaxHealth}";
            enemiesText.text = $"Enemies: {gameController.EnemiesAlive}";
            bool ended = gameController.Status != GameStatus.Playing;
            endPanel.SetActive(ended);
            if (ended)
            {
                endText.text = gameController.Status == GameStatus.Won ? "Victory" : "Defeat";
            }
        }
    }
}
