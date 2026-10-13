Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class K3 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  [DllImport("user32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  public static IntPtr Find(uint want) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p);
      if (p == want && IsWindowVisible(h)) { found = h; return false; } return true; }, IntPtr.Zero);
    return found;
  }
}
"@
$log = "C:\iso_director\f12v3.log"
$sb = ""
$cs = Get-Process cs2 -ErrorAction SilentlyContinue | Select-Object -First 1
$h = [K3]::Find([uint32]$cs.Id)
$sb += "cs2pid=$($cs.Id) hwnd=$h`n"
[void][K3]::ShowWindow($h, 9)
[void][K3]::BringWindowToTop($h)
$fg = [K3]::GetForegroundWindow()
$fgtid = 0; [void][K3]::GetWindowThreadProcessId($fg, [ref]$fgtid)
$mytid = [K3]::GetCurrentThreadId()
[void][K3]::AttachThreadInput($fgtid, $mytid, $true)
# ALT tap releases the foreground lock
[K3]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 40
[K3]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero); Start-Sleep -Milliseconds 60
$ok = [K3]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 400
$ok2 = [K3]::SetForegroundWindow($h)
[void][K3]::AttachThreadInput($fgtid, $mytid, $false)
$fgn = [K3]::GetForegroundWindow()
$sb += "attach ok=$ok/$ok2  fgnow=$fgn iscs2=$($fgn -eq $h)`n"
for ($i = 0; $i -lt 2; $i++) {
  [K3]::keybd_event(0x7B, 0x58, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 70
  [K3]::keybd_event(0x7B, 0x58, 2, [UIntPtr]::Zero); Start-Sleep -Milliseconds 500
}
$sb += "F12 x2 sent`n"
Set-Content $log $sb -Encoding ASCII
