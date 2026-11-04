using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

internal static class Launcher {
    [STAThread]
    private static int Main() {
        try {
            // Each run gets its own extraction directory; no stale partial script.
            string folder = Path.Combine(Path.GetTempPath(), "kid-setup-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(folder);
            string script = Path.Combine(folder, "loader.ps1");
            using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream("loader.ps1"))
            using (FileStream output = File.Create(script)) { input.CopyTo(output); }
            string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe");
            var start = new ProcessStartInfo(powershell, "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"");
            start.UseShellExecute = true;
            using (Process process = Process.Start(start)) { process.WaitForExit(); return process.ExitCode; }
        } catch (Exception error) {
            MessageBox.Show(error.Message, "kid setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
