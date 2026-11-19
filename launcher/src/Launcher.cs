// NIFTY_PaperTrading.exe - starts "python run_ml_demo.py" hidden, waits until the server answers,
// then opens http://127.0.0.1:8765/ml. Synthetic Mode, invented data, simulation only.
//
// Options (for testing):  --check   only report what would happen; start nothing
//                         --silent  no message boxes and no browser; use log + exit code
// Exit codes: 0 ok | 2 application folder not found | 3 Python missing or older than 3.12
//             4 port 8765 used by another program | 5 startup failed | 6 server not in Synthetic Mode

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static int Main(string[] args)
    {
        bool check = Array.IndexOf(args, "--check") >= 0;
        Common.Silent = Array.IndexOf(args, "--silent") >= 0;
        Common.Log("---- launcher started" + (check ? " (--check)" : "") + (Common.Silent ? " (--silent)" : ""));

        // One launcher at a time: a second double-click waits, then simply finds the running server.
        bool created;
        using (Mutex mutex = new Mutex(false, "Local\\NIFTY_PaperTrading_Launcher", out created))
        {
            bool held = false;
            try { held = mutex.WaitOne(90000); } catch (AbandonedMutexException) { held = true; }
            if (!held) { Common.Show("Another launcher window is still starting the dashboard. Please wait a moment and try again.", true); return 5; }
            try { return Run(check); }
            finally { mutex.ReleaseMutex(); }
        }
    }

    private static int Run(bool check)
    {
        // 1. Is the dashboard already running?
        bool synthetic;
        Probe state = Common.ProbeServer(out synthetic);
        List<int> owners = Common.ListenerPids();
        bool ownedByUs = false;
        foreach (int pid in owners)
        {
            string n, c; Common.ProcInfo(pid, out n, out c);
            if (Common.IsOurPython(n, c)) ownedByUs = true;
        }
        Common.Log("probe: " + state + ", synthetic=" + synthetic + ", listeners=" + owners.Count + ", ownedByNiftyPython=" + ownedByUs);

        if (state == Probe.OurServer)
        {
            if (!synthetic)
            {
                Common.Show("A dashboard is already running on port 8765, but it is NOT in Synthetic Mode.\n\n" +
                            "Close it with Stop_Dashboard.exe, then start NIFTY_PaperTrading.exe again.", true);
                return 6;
            }
            if (check) { Common.Log("check: already running in Synthetic Mode; would just open the browser"); return 0; }
            Common.Log("already running: opening the browser only, no second server");
            OpenBrowser();
            return 0;
        }
        if (state == Probe.OtherProgram && !ownedByUs)
        {
            Common.Show("Port 8765 is already used by another program, so the dashboard cannot start.\n\n" +
                        "Close that program (or restart the PC) and try again. Nothing was changed.", true);
            return 4;
        }

        // 2. Where is the application?
        List<string> tried = new List<string>();
        string appDir = Common.FindAppDir(tried);
        if (appDir == null)
        {
            Common.Show("Could not find the application folder (the folder that contains run_ml_demo.py).\n\n" +
                        "Keep this program next to the folder NIFTY-Options-ML-Paper-Trading, or put the path of the\n" +
                        "'application' folder on one line in a file named app_dir.txt next to this program.\n\n" +
                        "Details: " + Common.LauncherLog, true);
            Common.Log("looked in: " + string.Join(" ; ", tried.ToArray()));
            return 2;
        }
        Common.Log("application folder: " + appDir);

        // 3. Python 3.12+
        Common.PyInfo py = Common.FindPython();
        if (py == null)
        {
            Common.Show("Python was not found on this PC.\n\nInstall Python 3.12 or newer from python.org (tick \"Add python.exe to PATH\"), " +
                        "then run this program again. The Microsoft Store 'python' shortcut does not count.", true);
            return 3;
        }
        if (py.Major < 3 || (py.Major == 3 && py.Minor < 12))
        {
            Common.Show("Python " + py.Found + " was found, but the application needs Python 3.12 or newer.\n\nInstall a newer Python from python.org and try again.", true);
            return 3;
        }
        Common.Log("python: " + py.Exe + " (" + py.Found + ")");

        if (check) { Common.Log("check: OK, would start the server in " + appDir); return 0; }

        // 4. Start the server hidden. cmd.exe only supplies the log redirection; the server keeps running
        //    after this launcher exits.
        try { Directory.CreateDirectory(Common.LogDir); } catch { }
        long logOffset = 0;
        try { if (File.Exists(Common.ServerLog)) logOffset = new FileInfo(Common.ServerLog).Length; } catch { }
        Process proc = null;
        if (!(state == Probe.OtherProgram && ownedByUs))   // a python of ours is already starting: just wait for it
        {
            ProcessStartInfo psi = new ProcessStartInfo("cmd.exe",
                "/c \"\"" + py.Exe + "\" run_ml_demo.py >> \"" + Common.ServerLog + "\" 2>&1\"");
            psi.WorkingDirectory = appDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.EnvironmentVariables["PYTHONUTF8"] = "1";          // the app prints a rupee sign; keep the log readable
            psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
            try { proc = Process.Start(psi); }
            catch (Exception ex) { Common.Show("Could not start Python: " + ex.Message, true); return 5; }
            Common.Log("started server wrapper pid " + proc.Id);
        }

        // 5. Wait until the server really answers (not just until the process exists).
        Form wait = null; Label lbl = null;
        if (!Common.Silent)
        {
            wait = new Form();
            wait.Text = Common.Title;
            wait.FormBorderStyle = FormBorderStyle.FixedDialog;
            wait.MaximizeBox = false; wait.MinimizeBox = false; wait.ControlBox = false;
            wait.StartPosition = FormStartPosition.CenterScreen;
            wait.ClientSize = new Size(380, 70);
            lbl = new Label();
            lbl.Dock = DockStyle.Fill; lbl.TextAlign = ContentAlignment.MiddleCenter;
            lbl.Text = "Starting the dashboard (simulated data)...";
            wait.Controls.Add(lbl);
            wait.Show();
        }

        DateTime deadline = DateTime.UtcNow.AddSeconds(60);
        bool ready = false; bool synth = false;
        while (DateTime.UtcNow < deadline)
        {
            if (wait != null) Application.DoEvents();
            bool s; Probe p = Common.ProbeServer(out s);
            if (p == Probe.OurServer) { ready = true; synth = s; break; }
            if (proc != null && Common.ListenerPids().Count == 0 && ServerWrapperGone(proc)) break;   // python quit early
            Thread.Sleep(250);
        }
        if (wait != null) { wait.Close(); wait.Dispose(); }

        if (!ready)
        {
            string tail = Common.LogTail(logOffset, 12);
            Common.Show("The dashboard did not start.\n\n" +
                        (tail.Length > 0 ? "The program said:\n" + tail + "\n\n" : "") +
                        "Full log: " + Common.ServerLog, true);
            return 5;
        }
        if (!synth)
        {
            Common.Show("The server started but is not in Synthetic Mode. Close it with Stop_Dashboard.exe.", true);
            return 6;
        }
        Common.Log("server ready in Synthetic Mode");
        OpenBrowser();
        return 0;
    }

    private static bool ServerWrapperGone(Process p)
    {
        try { return p.HasExited; } catch { return true; }
    }

    private static void OpenBrowser()
    {
        if (Common.Silent) { Common.Log("silent: not opening the browser (" + Common.OpenUrl + ")"); return; }
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(Common.OpenUrl);
            psi.UseShellExecute = true;
            Process.Start(psi);
        }
        catch (Exception ex)
        {
            Common.Show("The dashboard is running, but the browser could not be opened.\nOpen this address yourself:\n" + Common.OpenUrl + "\n\n" + ex.Message, true);
        }
    }
}
