# 受管作业对象：把子进程放进带 KILL_ON_JOB_CLOSE 的 Windows 作业对象。
# 只要启动脚本所在的进程结束——正常退出、Ctrl+C、窗口被关闭、被任务管理器强杀——
# 系统都会连带结束作业内的所有子进程，避免 Python/Go 服务留在后台。
if (-not ('MoneyRouter.NativeJob' -as [type])) {
 Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace MoneyRouter {
 [StructLayout(LayoutKind.Sequential)]
 public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
  public long PerProcessUserTimeLimit;
  public long PerJobUserTimeLimit;
  public uint LimitFlags;
  public UIntPtr MinimumWorkingSetSize;
  public UIntPtr MaximumWorkingSetSize;
  public uint ActiveProcessLimit;
  public UIntPtr Affinity;
  public uint PriorityClass;
  public uint SchedulingClass;
 }

 [StructLayout(LayoutKind.Sequential)]
 public struct IO_COUNTERS {
  public ulong ReadOperationCount;
  public ulong WriteOperationCount;
  public ulong OtherOperationCount;
  public ulong ReadTransferCount;
  public ulong WriteTransferCount;
  public ulong OtherTransferCount;
 }

 [StructLayout(LayoutKind.Sequential)]
 public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
  public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
  public IO_COUNTERS IoInfo;
  public UIntPtr ProcessMemoryLimit;
  public UIntPtr JobMemoryLimit;
  public UIntPtr PeakProcessMemoryUsed;
  public UIntPtr PeakJobMemoryUsed;
 }

 [StructLayout(LayoutKind.Sequential)]
 public struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION {
  public long TotalUserTime;
  public long TotalKernelTime;
  public long ThisPeriodTotalUserTime;
  public long ThisPeriodTotalKernelTime;
  public uint TotalPageFaultCount;
  public uint TotalProcesses;
  public uint ActiveProcesses;
  public uint TotalTerminatedProcesses;
 }

 public static class NativeJob {
  public const int JobObjectBasicAccountingInformation = 1;
  public const int JobObjectExtendedLimitInformation = 9;
  public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool SetInformationJobObject(IntPtr hJob, int infoClass, IntPtr lpInfo, uint cbInfo);

  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool QueryInformationJobObject(IntPtr hJob, int infoClass, IntPtr lpInfo, uint cbInfo, IntPtr lpReturnLength);

  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool IsProcessInJob(IntPtr hProcess, IntPtr hJob, out bool result);

  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool CloseHandle(IntPtr hObject);

  public static IntPtr CreateKillOnCloseJob() {
   IntPtr job = CreateJobObject(IntPtr.Zero, null);
   if (job == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
   JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
   info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
   int size = Marshal.SizeOf(info);
   IntPtr buffer = Marshal.AllocHGlobal(size);
   try {
    Marshal.StructureToPtr(info, buffer, false);
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, buffer, (uint)size)) {
     int error = Marshal.GetLastWin32Error();
     CloseHandle(job);
     throw new System.ComponentModel.Win32Exception(error);
    }
   } finally { Marshal.FreeHGlobal(buffer); }
   return job;
  }

  public static void Assign(IntPtr job, IntPtr processHandle) {
   if (!AssignProcessToJobObject(job, processHandle)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
  }

  public static bool Contains(IntPtr job, IntPtr processHandle) {
   bool result;
   if (!IsProcessInJob(processHandle, job, out result)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
   return result;
  }

  public static uint ActiveProcesses(IntPtr job) {
   JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info = new JOBOBJECT_BASIC_ACCOUNTING_INFORMATION();
   int size = Marshal.SizeOf(info);
   IntPtr buffer = Marshal.AllocHGlobal(size);
   try {
    Marshal.StructureToPtr(info, buffer, false);
    if (!QueryInformationJobObject(job, JobObjectBasicAccountingInformation, buffer, (uint)size, IntPtr.Zero)) {
     throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    return Marshal.PtrToStructure<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>(buffer).ActiveProcesses;
   } finally { Marshal.FreeHGlobal(buffer); }
  }
 }
}
'@
}

function New-KillOnCloseJob {
 [CmdletBinding()]
 param()
 return [MoneyRouter.NativeJob]::CreateKillOnCloseJob()
}

function Add-ProcessToJobObject {
 # 加入失败不影响主流程：仍由 finally 里的显式停止兜底，但要提示用户用 stop.cmd 收尾。
 [CmdletBinding()]
 param(
  [Parameter(Mandatory=$true)]$Process,
  [Parameter(Mandatory=$true)][IntPtr]$JobHandle,
  [string]$Label='子进程'
 )
 try {
  [MoneyRouter.NativeJob]::Assign($JobHandle, $Process.Handle)
  return $true
 } catch {
  Write-Warning "$Label 未能加入受管作业（$($_.Exception.Message)）。窗口关闭后可能残留，请运行 stop.cmd 停止。"
  return $false
 }
}

function Close-JobObject {
 [CmdletBinding()]
 param([IntPtr]$JobHandle)
 if ($JobHandle -and $JobHandle -ne [IntPtr]::Zero) { [void][MoneyRouter.NativeJob]::CloseHandle($JobHandle) }
}
