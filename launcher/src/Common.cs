// Shared helpers for NIFTY_PaperTrading.exe and Stop_Dashboard.exe.
// Compiled with the C# compiler that ships with Windows (no extra tools, no Python packaging).
// Simulation only: this file never talks to a broker, a data provider or the internet.
// It only runs "python run_ml_demo.py" on this PC and talks to 127.0.0.1.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

internal enum Probe { Down, OurServer, OtherProgram }

internal static class Common
{
    public const int Port = 8765;                       // the application's default port
    public const string Base = "http://127.0.0.1:8765";
    public const string OpenUrl = "http://127.0.0.1:8765/ml";
    public const string Title = "NIFTY Paper Trading";

    // --silent: no message boxes, no browser; results go to the log and the exit code (used for testing).
    public static bool Silent;

    public static string LogDir
    {
        get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "NIFTY_PaperTrading"); }
    }
    public static string LauncherLog { get { return Path.Combine(LogDir, "launcher.log"); } }
    public static string ServerLog { get { return Path.Combine(LogDir, "server.log"); } }

    public static void Log(string msg)
    {
        try
        {
            Directory.CreateDirectory(LogDir);
            File.AppendAllText(LauncherLog, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + msg + Environment.NewLine);
        }
        catch { }
    }

    public static void Show(string text, bool error)
    {
        Log((error ? "ERROR: " : "INFO: ") + text.Replace(Environment.NewLine, " | "));
        if (Silent) return;
        MessageBox.Show(text, Title, MessageBoxButtons.OK, error ? MessageBoxIcon.Error : MessageBoxIcon.Information);
    }

    // ---------- where is the application? (never a hard-coded user path) ----------
    public static bool IsAppDir(string dir)
    {
        return !string.IsNullOrEmpty(dir)
            && File.Exists(Path.Combine(dir, "run_ml_demo.py"))
            && Directory.Exists(Path.Combine(dir, "src", "niftypaper"));
    }

    public static string FindAppDir(List<string> tried)
    {
        string exeDir = AppDomain.CurrentDomain.BaseDirectory;
        List<string> cands = new List<string>();

        string env = Environment.GetEnvironmentVariable("NIFTY_APP_DIR");
        if (!string.IsNullOrEmpty(env)) cands.Add(env);

        string cfg = Path.Combine(exeDir, "app_dir.txt");           // written by build.ps1 -Install
        if (File.Exists(cfg))
        {
            foreach (string line in File.ReadAllLines(cfg))
                if (line.Trim().Length > 0) { cands.Add(line.Trim()); break; }
        }

        // launched from inside the project (launcher\dist\...): walk upwards
        DirectoryInfo d = new DirectoryInfo(exeDir);
        for (int i = 0; d != null && i < 6; i++, d = d.Parent)
        {
            cands.Add(d.FullName);
            cands.Add(Path.Combine(d.FullName, "application"));
        }

        // exe sitting next to the project folder (for example on the Desktop)
        cands.Add(Path.Combine(exeDir, "NIFTY-Options-ML-Paper-Trading", "application"));

        foreach (string c in cands)
        {
            string full;
            try { full = Path.GetFullPath(c); } catch { continue; }
            if (tried != null) tried.Add(full);
            if (IsAppDir(full)) return full;
        }
        return null;
    }

    // ---------- Python 3.12+ ----------
    public class PyInfo { public string Exe; public int Major; public int Minor; public string Found = ""; }

    public static PyInfo FindPython()
    {
        string[][] tries = new string[][] {
            new string[] { "py", "-3" },
            new string[] { "python", "" },
            new string[] { "python3", "" }
        };
        PyInfo best = null;
        foreach (string[] t in tries)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(t[0],
                    (t[1].Length > 0 ? t[1] + " " : "") + "-c \"import sys; print(sys.version_info[0], sys.version_info[1]); print(sys.executable)\"");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                using (Process p = Process.Start(psi))
                {
                    string err = null;
                    p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { err += e.Data; };
                    p.BeginErrorReadLine();
                    string o = p.StandardOutput.ReadToEnd();
                    if (!p.WaitForExit(15000)) { try { p.Kill(); } catch { } continue; }
                    if (p.ExitCode != 0) continue;           // e.g. the Microsoft Store "python" stub
                    string[] lines = o.Split(new char[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
                    if (lines.Length < 2) continue;
                    string[] ver = lines[0].Split(' ');
                    PyInfo pi = new PyInfo();
                    pi.Major = int.Parse(ver[0]); pi.Minor = int.Parse(ver[1]);
                    pi.Exe = lines[1].Trim();
                    pi.Found = pi.Major + "." + pi.Minor;
                    if (pi.Major > 3 || (pi.Major == 3 && pi.Minor >= 12)) return pi;
                    if (best == null) best = pi;
                }
            }
            catch (Exception ex) { Log("python probe '" + t[0] + "' failed: " + ex.Message); }
        }
        return best;   // null: nothing usable found; non-null: found but too old
    }

    // ---------- what is on port 8765? ----------
    public static bool PortOpen()
    {
        try
        {
            using (TcpClient c = new TcpClient())
            {
                IAsyncResult ar = c.BeginConnect(IPAddress.Loopback, Port, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(600)) return false;
                c.EndConnect(ar);
                return true;
            }
        }
        catch { return false; }
    }

    // Asks 127.0.0.1 only. Recognises our server by the banner the app puts in its own snapshot.
    public static Probe ProbeServer(out bool synthetic)
    {
        synthetic = false;
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(Base + "/api/snapshot");
            req.Proxy = null;                       // never go through a system proxy
            req.Timeout = 2500;
            req.ReadWriteTimeout = 2500;
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
            using (StreamReader sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            {
                string body = sr.ReadToEnd();
                if (body.Contains("NOT LIVE MARKET DATA") && body.Contains("\"mode\""))
                {
                    synthetic = Regex.IsMatch(body, "\"mode\"\\s*:\\s*\"synthetic\"");
                    return Probe.OurServer;
                }
                return Probe.OtherProgram;
            }
        }
        catch { return PortOpen() ? Probe.OtherProgram : Probe.Down; }
    }

    // ---------- who owns the listening socket? ----------
    [DllImport("iphlpapi.dll", SetLastError = true)]
    private static extern uint GetExtendedTcpTable(IntPtr table, ref int size, bool sort, int af, int cls, uint reserved);

    public static List<int> ListenerPids()
    {
        List<int> pids = new List<int>();
        int size = 0;
        GetExtendedTcpTable(IntPtr.Zero, ref size, true, 2, 3, 0);           // AF_INET, TCP_TABLE_OWNER_PID_LISTENER
        IntPtr buf = Marshal.AllocHGlobal(size);
        try
        {
            if (GetExtendedTcpTable(buf, ref size, true, 2, 3, 0) != 0) return pids;
            int n = Marshal.ReadInt32(buf);
            for (int i = 0; i < n; i++)
            {
                IntPtr row = new IntPtr(buf.ToInt64() + 4 + i * 24);          // MIB_TCPROW_OWNER_PID = 6 x uint
                uint rawPort = (uint)Marshal.ReadInt32(row, 8);
                int port = (int)(((rawPort & 0xFF) << 8) | ((rawPort >> 8) & 0xFF));
                int pid = Marshal.ReadInt32(row, 20);
                if (port == Port && !pids.Contains(pid)) pids.Add(pid);
            }
        }
        finally { Marshal.FreeHGlobal(buf); }
        return pids;
    }

    public static void ProcInfo(int pid, out string name, out string cmd)
    {
        name = ""; cmd = "";
        try
        {
            using (ManagementObjectSearcher s = new ManagementObjectSearcher("SELECT Name, CommandLine FROM Win32_Process WHERE ProcessId=" + pid))
                foreach (ManagementObject m in s.Get())
                {
                    name = (m["Name"] as string) ?? "";
                    cmd = (m["CommandLine"] as string) ?? "";
                }
        }
        catch (Exception ex) { Log("WMI lookup failed: " + ex.Message); }
    }

    // True only for a python process running this project's run_ml_demo.py / run_dashboard.py.
    public static bool IsOurPython(string name, string cmd)
    {
        return name.ToLowerInvariant().StartsWith("python")
            && (cmd.Contains("run_ml_demo.py") || cmd.Contains("run_dashboard.py"));
    }

    public static string LogTail(long fromOffset, int maxLines)
    {
        try
        {
            using (FileStream fs = new FileStream(ServerLog, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            {
                if (fromOffset > fs.Length) fromOffset = 0;
                fs.Seek(fromOffset, SeekOrigin.Begin);
                string txt = new StreamReader(fs, Encoding.UTF8).ReadToEnd().Trim();
                string[] lines = txt.Split('\n');
                int start = Math.Max(0, lines.Length - maxLines);
                return string.Join("\n", lines, start, lines.Length - start).Trim();
            }
        }
        catch { return ""; }
    }
}
