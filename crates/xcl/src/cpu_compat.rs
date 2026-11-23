//! Guest CPU instructions unavailable on some x64 hosts.

use std::ffi::c_void;
use std::mem::size_of;
use std::sync::atomic::{AtomicU64, Ordering};

use windows_sys::Win32::Foundation::{GetLastError, SetLastError, EXCEPTION_ILLEGAL_INSTRUCTION};
use windows_sys::Win32::System::Diagnostics::Debug::{
    AddVectoredExceptionHandler, ReadProcessMemory, WriteProcessMemory, CONTEXT, EXCEPTION_POINTERS,
};
use windows_sys::Win32::System::Memory::{
    VirtualQuery, MEMORY_BASIC_INFORMATION, MEM_COMMIT, PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY, PAGE_GUARD, PAGE_READWRITE, PAGE_WRITECOPY,
};
use windows_sys::Win32::System::SystemServices::EXCEPTION_NONCONTINUABLE;
use windows_sys::Win32::System::Threading::{GetCurrentProcess, SwitchToThread};

// AMD64 Architecture Programmer's Manual, volume 3.
const MONITORX: [u8; 3] = [0x0f, 0x01, 0xfa];
const MWAITX: [u8; 3] = [0x0f, 0x01, 0xfb];
const CLZERO: [u8; 3] = [0x0f, 0x01, 0xfc];
const CACHE_LINE_BYTES: usize = 64;
static EMULATED_CLZERO: AtomicU64 = AtomicU64::new(0);
static EMULATED_MWAITX: AtomicU64 = AtomicU64::new(0);

pub(crate) fn emulated_clzero() -> u64 {
    EMULATED_CLZERO.load(Ordering::Relaxed)
}

pub(crate) fn emulated_mwaitx() -> u64 {
    EMULATED_MWAITX.load(Ordering::Relaxed)
}

pub(crate) fn install() {
    unsafe {
        // MONITORX can fault in user mode even when advertised by the host CPU.
        // Instructions that execute successfully on the host never enter the handler.
        if AddVectoredExceptionHandler(0, Some(handle_illegal_instruction)).is_null() {
            crate::xlog_warn!("could not install the guest CPU instruction fallbacks");
        } else {
            crate::xlog_info!("guest CPU instruction fallbacks enabled");
        }
    }
}

unsafe extern "system" fn handle_illegal_instruction(exception: *mut EXCEPTION_POINTERS) -> i32 {
    let record = &*(*exception).ExceptionRecord;
    if record.ExceptionCode != EXCEPTION_ILLEGAL_INSTRUCTION
        || record.ExceptionFlags & EXCEPTION_NONCONTINUABLE != 0
    {
        return 0; // EXCEPTION_CONTINUE_SEARCH
    }
    let context = &mut *(*exception).ContextRecord;
    if crate::modules::image_base_for_addr(context.Rip as usize).is_none() {
        return 0;
    }
    let last_error = GetLastError();
    let handled = emulate_guest_instruction(context);
    SetLastError(last_error);
    if handled {
        -1 // EXCEPTION_CONTINUE_EXECUTION
    } else {
        0
    }
}

unsafe fn emulate_guest_instruction(context: &mut CONTEXT) -> bool {
    let process = GetCurrentProcess();
    let mut opcode = [0; CLZERO.len()];
    let mut transferred = 0;
    if ReadProcessMemory(process, context.Rip as *const c_void, opcode.as_mut_ptr().cast(),
                         opcode.len(), &mut transferred) == 0
        || transferred != opcode.len()
    {
        return false;
    }

    match opcode {
        CLZERO => {
            if !zero_cache_line(process, context.Rax) {
                return false;
            }
            EMULATED_CLZERO.fetch_add(1, Ordering::Relaxed);
        }
        MONITORX if context.Rcx as u32 == 0 && context.Rdx as u32 == 0 => {
            // Check the address. The paired MWAITX yields and returns early, so
            // the title rechecks the condition without a hardware monitor.
            let mut byte = 0u8;
            if ReadProcessMemory(process, context.Rax as *const c_void, (&mut byte as *mut u8).cast(),
                                 1, &mut transferred) == 0 || transferred != 1
            {
                return false;
            }
        }
        MWAITX if context.Rax as u32 == 0 && context.Rcx as u32 & !3 == 0 => {
            // Hardware waits may return without a write. Yield once, then let
            // the guest's wait loop recheck its condition and timeout.
            SwitchToThread();
            EMULATED_MWAITX.fetch_add(1, Ordering::Relaxed);
        }
        _ => return false,
    }
    context.Rip += opcode.len() as u64;
    true
}

unsafe fn zero_cache_line(process: windows_sys::Win32::Foundation::HANDLE, rax: u64) -> bool {
    // Zen 2 ignores the low six address bits. Use ordinary stores via the checked
    // Windows API and leave RIP unchanged if the destination cannot be written.
    let line = ((rax as usize) & !(CACHE_LINE_BYTES - 1)) as *mut c_void;
    let mut page = MEMORY_BASIC_INFORMATION::default();
    const WRITABLE: u32 = PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
    if VirtualQuery(line, &mut page, size_of::<MEMORY_BASIC_INFORMATION>()) == 0 || page.State != MEM_COMMIT
        || page.Protect & PAGE_GUARD != 0 || page.Protect & WRITABLE == 0
    {
        return false;
    }
    let zeros = [0u8; CACHE_LINE_BYTES];
    let mut transferred = 0;
    WriteProcessMemory(process, line, zeros.as_ptr().cast(), zeros.len(), &mut transferred) != 0
        && transferred == zeros.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clears_only_the_aligned_guest_line_and_advances_rip() {
        #[repr(align(64))]
        struct Lines([u8; CACHE_LINE_BYTES * 3]);

        let mut lines = Lines([0x7f; CACHE_LINE_BYTES * 3]);
        let opcode = CLZERO;
        let mut context = CONTEXT {
            Rip: opcode.as_ptr() as u64,
            Rax: unsafe { lines.0.as_mut_ptr().add(CACHE_LINE_BYTES + 15) } as u64,
            EFlags: 0x202,
            ..Default::default()
        };
        let original_rax = context.Rax;
        assert!(unsafe { emulate_guest_instruction(&mut context) });
        assert_eq!(context.Rip, opcode.as_ptr() as u64 + 3);
        assert_eq!(context.Rax, original_rax);
        assert_eq!(context.EFlags, 0x202);
        assert_eq!(lines.0[..CACHE_LINE_BYTES], [0x7f; CACHE_LINE_BYTES]);
        assert_eq!(lines.0[CACHE_LINE_BYTES..CACHE_LINE_BYTES * 2], [0; CACHE_LINE_BYTES]);
        assert_eq!(lines.0[CACHE_LINE_BYTES * 2..], [0x7f; CACHE_LINE_BYTES]);

        let other = [0x0f, 0x0b, 0x90];
        context.Rip = other.as_ptr() as u64;
        assert!(!unsafe { emulate_guest_instruction(&mut context) });
        assert_eq!(context.Rip, other.as_ptr() as u64);
        context.Rip = opcode.as_ptr() as u64;
        context.Rax = 0;
        assert!(!unsafe { emulate_guest_instruction(&mut context) });
        assert_eq!(context.Rip, opcode.as_ptr() as u64);
    }

    #[test]
    fn monitor_wait_fallback_preserves_flags_and_registers() {
        let opcode = [MONITORX, MWAITX].concat();
        let address = [0x42u8];
        let mut context = CONTEXT {
            Rip: opcode.as_ptr() as u64,
            Rax: address.as_ptr() as u64,
            Rbx: 1000,
            EFlags: 0x202,
            ..Default::default()
        };
        assert!(unsafe { emulate_guest_instruction(&mut context) });
        assert_eq!(context.Rax, address.as_ptr() as u64);
        context.Rax = 0;
        context.Rcx = 2;
        assert!(unsafe { emulate_guest_instruction(&mut context) });
        assert_eq!(context.Rip, opcode.as_ptr() as u64 + 6);
        assert_eq!((context.Rax, context.Rbx, context.Rcx, context.EFlags), (0, 1000, 2, 0x202));
    }
}
