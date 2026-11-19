// Stop_Dashboard.exe - stops the NIFTY paper-trading dashboard server (python run_ml_demo.py / run_dashboard.py).
// It finds the program listening on 127.0.0.1:8765, checks that it really is this project's Python server,
// asks it to quit with Ctrl+C (the same as pressing Ctrl+C in a window), and only force-closes it if that fails.
// It never touches saved replays or the virtual account files. Option: --silent (no message boxes).

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

internal static class StopDashboard
{
    [DllImport("kernel32.dll")] private static extern bool FreeConsole();
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AttachConsole(uint pid);
    [DllImport("kernel32.dll")] private static extern bool GenerateConsoleCtrlEvent(uint evt, uint group);
    [DllImport("kernel32.dll")] private static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);

    [STAThread]
    private static int Main(string[] args)
    {
        Common.Silent = Array.IndexOf(args, "--silent") >= 0;
        Common.Log("---- stop requested");

        List<int> pids = Common.ListenerPids();
        if (pids.Count == 0)
        {
            Common.Show("The dashboard is not running (nothing is using port 8765).", false);
            return 0;
        }

        foreach (int pid in pids)
        {
            string name, cmd; Common.ProcInfo(pid, out name, out cmd);
            if (!Common.IsOurPython(name, cmd))
            {
                Common.Show("Port 8765 is used by another program (" + name + ", process " + pid + "), not by the NIFTY dashboard.\nI did not stop it.", true);
                return 4;
            }
            Common.Log("stopping " + name + " pid " + pid);
            if (!AskToQuit(pid) || !WaitPortFree(8000))
            {
                Common.Log("Ctrl+C did not stop it; forcing the process tree to close");
                try
                {
                    ProcessStartInfo k = new ProcessStartInfo("taskkill", "/PID " + pid + " /T /F");
                    k.CreateNoWindow = true; k.UseShellExecute = false;
                    using (Process p = Process.Start(k)) p.WaitForExit(10000);
                }
                catch (Exception ex) { Common.Log("taskkill failed: " + ex.Message); }
            }
        }

        if (WaitPortFree(8000))
        {
            Common.Show("The dashboard has been stopped. Saved replays and your virtual account were not touched.", false);
            return 0;
        }
        Common.Show("The dashboard is still running. Try again, or restart the PC.", true);
        return 5;
    }

    // Sends Ctrl+C to the (hidden) console the server runs in. Ignores the signal in this process.
    private static bool AskToQuit(int pid)
    {
        try
        {
            FreeConsole();
            if (!AttachConsole((uint)pid)) { Common.Log("AttachConsole failed: " + Marshal.GetLastWin32Error()); return false; }
            SetConsoleCtrlHandler(IntPtr.Zero, true);
            bool ok = GenerateConsoleCtrlEvent(0, 0);
            Thread.Sleep(300);
            FreeConsole();
            return ok;
        }
        catch (Exception ex) { Common.Log("Ctrl+C failed: " + ex.Message); return false; }
    }

    private static bool WaitPortFree(int ms)
    {
        DateTime end = DateTime.UtcNow.AddMilliseconds(ms);
        while (DateTime.UtcNow < end)
        {
            if (Common.ListenerPids().Count == 0) return true;
            Thread.Sleep(200);
        }
        return Common.ListenerPids().Count == 0;
    }
}
