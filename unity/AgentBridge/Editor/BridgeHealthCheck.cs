using System;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace VerifiedGameBuilder.AgentBridge.Editor
{
    public static class BridgeHealthCheck
    {
        public const string Version = "0.1.0";

        [MenuItem("GameForge/Health Check")]
        public static void LogHealthCheck()
        {
            Debug.Log($"GameForge Agent Bridge {Version}: healthy");
        }

        public static void WriteBatchHealthCheck()
        {
            string outputPath = ReadOutputPath();
            string directory = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            string json = JsonUtility.ToJson(new HealthPayload
            {
                status = "healthy",
                bridgeVersion = Version,
                unityVersion = Application.unityVersion,
                timestamp = DateTime.UtcNow.ToString("O")
            }, true);
            File.WriteAllText(outputPath, json + Environment.NewLine);
        }

        private static string ReadOutputPath()
        {
            string[] arguments = Environment.GetCommandLineArgs();
            for (int index = 0; index < arguments.Length - 1; index++)
            {
                if (arguments[index] == "-gameforgeOutput")
                {
                    return Path.GetFullPath(arguments[index + 1]);
                }
            }

            throw new ArgumentException("Missing -gameforgeOutput <path> argument.");
        }

        [Serializable]
        private sealed class HealthPayload
        {
            public string status = string.Empty;
            public string bridgeVersion = string.Empty;
            public string unityVersion = string.Empty;
            public string timestamp = string.Empty;
        }
    }
}
