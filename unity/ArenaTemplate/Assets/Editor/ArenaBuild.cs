using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace VerifiedGameBuilder.Editor
{
    public static class ArenaBuild
    {
        public static void BuildMacOS()
        {
            string outputPath = ReadArgument("-gameforgeOutput");
            string parent = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(parent))
            {
                Directory.CreateDirectory(parent);
            }

            string[] scenes = EditorBuildSettings.scenes
                .Where(scene => scene.enabled)
                .Select(scene => scene.path)
                .ToArray();
            if (scenes.Length == 0)
            {
                throw new InvalidOperationException("No enabled build scenes were found.");
            }

            BuildPlayerOptions options = new BuildPlayerOptions
            {
                scenes = scenes,
                locationPathName = outputPath,
                target = BuildTarget.StandaloneOSX,
                options = BuildOptions.Development,
            };
            BuildReport report = BuildPipeline.BuildPlayer(options);
            if (report.summary.result != BuildResult.Succeeded)
            {
                throw new InvalidOperationException(
                    $"macOS build failed: {report.summary.result}, " +
                    $"errors={report.summary.totalErrors}");
            }

            Debug.Log(
                $"macOS build succeeded: {outputPath}, " +
                $"bytes={report.summary.totalSize}");
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

            throw new ArgumentException($"Missing {name} <path> argument.");
        }
    }
}
