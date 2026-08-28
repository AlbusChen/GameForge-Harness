using System;
using System.Collections.Generic;
using UnityEngine;

namespace VerifiedGameBuilder.AgentBridge
{
    [Serializable]
    public sealed class RuntimeSnapshot
    {
        public string timestamp = string.Empty;
        public int frame;
        public string gameStatus = "unknown";
        public PlayerSnapshot player = new PlayerSnapshot();
        public List<EnemySnapshot> enemies = new List<EnemySnapshot>();
        public int enemiesAlive;
        public int currentWave;
        public UiSnapshot ui = new UiSnapshot();
        public List<string> errors = new List<string>();
    }

    [Serializable]
    public sealed class PlayerSnapshot
    {
        public int health;
        public int maxHealth;
        public Vector3 position;
        public int shotsFired;
    }

    [Serializable]
    public sealed class EnemySnapshot
    {
        public string id = string.Empty;
        public int health;
        public Vector3 position;
        public string state = "unknown";
    }

    [Serializable]
    public sealed class UiSnapshot
    {
        public bool endScreenVisible;
        public string healthText = string.Empty;
    }
}
