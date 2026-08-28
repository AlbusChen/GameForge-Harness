using System;
using System.Collections;
using System.IO;
using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class BuildSmokeProbe : MonoBehaviour
    {
        private IEnumerator Start()
        {
            string output = ReadArgument("-gameforgeSmokeOutput");
            string checkpoint = ReadArgument("-gameforgeSmokeCheckpoint");
            if (string.IsNullOrEmpty(output))
            {
                yield break;
            }

            Application.runInBackground = true;
            WriteCheckpoint(checkpoint, "started");
            yield return null;
            yield return null;

            RuntimeStateProbe stateProbe = FindFirstObjectByType<RuntimeStateProbe>();
            if (stateProbe == null)
            {
                WriteCheckpoint(checkpoint, "state_probe_missing");
                Application.Quit(2);
                yield break;
            }

            EnsureParent(output);
            string json = stateProbe.CaptureJson();
            File.WriteAllText(output, json + Environment.NewLine);
            WriteCheckpoint(checkpoint, "state_written");

            string screenshot = ReadArgument("-gameforgeScreenshot");
            if (!string.IsNullOrEmpty(screenshot))
            {
                EnsureParent(screenshot);
                ScreenCapture.CaptureScreenshot(screenshot);
                DateTime deadline = DateTime.UtcNow.AddSeconds(10);
                while (!File.Exists(screenshot) && DateTime.UtcNow < deadline)
                {
                    yield return null;
                }
            }

            WriteCheckpoint(
                checkpoint,
                string.IsNullOrEmpty(screenshot) || File.Exists(screenshot)
                    ? "complete"
                    : "screenshot_missing"
            );
            Application.Quit(0);
        }

        private static void WriteCheckpoint(string path, string stage)
        {
            if (string.IsNullOrEmpty(path))
            {
                return;
            }

            EnsureParent(path);
            string json = "{\"stage\":\"" + stage + "\",\"updatedAt\":\""
                + DateTime.UtcNow.ToString("O") + "\"}";
            File.WriteAllText(path, json + Environment.NewLine);
        }

        private static string ReadArgument(string name)
        {
            string[] arguments = Environment.GetCommandLineArgs();
            for (int index = 0; index < arguments.Length - 1; index++)
            {
                if (arguments[index] == name)
                {
                    return Path.GetFullPath(arguments[index + 1]);
                }
            }

            return string.Empty;
        }

        private static void EnsureParent(string path)
        {
            string parent = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(parent))
            {
                Directory.CreateDirectory(parent);
            }
        }
    }
}
