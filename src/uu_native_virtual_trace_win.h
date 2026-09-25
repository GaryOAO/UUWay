#ifndef UURB_NATIVE_VIRTUAL_TRACE_WIN_H
#define UURB_NATIVE_VIRTUAL_TRACE_WIN_H
#include <setupapi.h>
/* Passive discovery trace scoped to UU's reviewed IDD interface. No device
 * names, handle values, monitor IDs or account fields. Never report a fake
 * installed driver or fabricate successful device enumeration. */
static const GUID uurb_idd_interface = {0xc5710012,0xf569,0x4079,{0x84,0x14,0x56,0x4a,0xb6,0x32,0x83,0x47}};
static HDEVINFO (WINAPI *virtual_class_devs_w)(const GUID *, PCWSTR, HWND, DWORD);
static HDEVINFO (WINAPI *virtual_class_devs_a)(const GUID *, PCSTR, HWND, DWORD);
static BOOL (WINAPI *virtual_enum_interfaces)(HDEVINFO, PSP_DEVINFO_DATA, const GUID *, DWORD, PSP_DEVICE_INTERFACE_DATA);
static volatile LONG virtual_trace_calls[3];
static BOOL virtual_guid_matches(const GUID *guid)
{
    GUID copy; SIZE_T bytes=0;
    return guid && ReadProcessMemory(GetCurrentProcess(),guid,&copy,sizeof(copy),&bytes) &&
        bytes==sizeof(copy) && !memcmp(&copy,&uurb_idd_interface,sizeof(copy));
}
static void virtual_discovery_trace(unsigned api, DWORD flags, DWORD index, BOOL success, DWORD error)
{
    if (InterlockedIncrement(&virtual_trace_calls[api]) <= 32) {
        const char *names[]={"SetupDiGetClassDevsW","SetupDiGetClassDevsA","SetupDiEnumDeviceInterfaces"};
        char line[256];
        snprintf(line,sizeof(line),"UURB_NATIVE_VIRTUAL {\"api\":\"%s\",\"flags\":%lu,\"index\":%lu,\"success\":%s,\"error\":%lu}\r\n",
            names[api],(unsigned long)flags,(unsigned long)index,success?"true":"false",(unsigned long)error);
        write_log(line);flush_log();
    }
}
static HDEVINFO WINAPI traced_virtual_class_w(const GUID *guid, PCWSTR enumerator, HWND parent, DWORD flags)
{
    DWORD saved=GetLastError(); BOOL matched=virtual_guid_matches(guid); SetLastError(saved);
    HDEVINFO result=virtual_class_devs_w(guid,enumerator,parent,flags);saved=GetLastError();
    if (matched) virtual_discovery_trace(0,flags,0,result!=INVALID_HANDLE_VALUE,saved);
    SetLastError(saved);return result;
}
static HDEVINFO WINAPI traced_virtual_class_a(const GUID *guid, PCSTR enumerator, HWND parent, DWORD flags)
{
    DWORD saved=GetLastError(); BOOL matched=virtual_guid_matches(guid); SetLastError(saved);
    HDEVINFO result=virtual_class_devs_a(guid,enumerator,parent,flags);saved=GetLastError();
    if (matched) virtual_discovery_trace(1,flags,0,result!=INVALID_HANDLE_VALUE,saved);
    SetLastError(saved);return result;
}
static BOOL WINAPI traced_virtual_enum(HDEVINFO set, PSP_DEVINFO_DATA device, const GUID *guid,
                                       DWORD index, PSP_DEVICE_INTERFACE_DATA output)
{
    DWORD saved=GetLastError(); BOOL matched=virtual_guid_matches(guid); SetLastError(saved);
    BOOL result=virtual_enum_interfaces(set,device,guid,index,output);saved=GetLastError();
    if (matched) virtual_discovery_trace(2,0,index,result,saved);
    SetLastError(saved);return result;
}
static void *native_virtual_hook_for(uintptr_t original)
{
    if (!original) return NULL;
    if (original==(uintptr_t)virtual_class_devs_w) return (void *)&traced_virtual_class_w;
    if (original==(uintptr_t)virtual_class_devs_a) return (void *)&traced_virtual_class_a;
    if (original==(uintptr_t)virtual_enum_interfaces) return (void *)&traced_virtual_enum;
    return NULL;
}
static void native_virtual_trace_initialize(void)
{
    HMODULE module=GetModuleHandleW(L"setupapi.dll");
    if (!module) return; /* Do not load a driver/library just for diagnostics. */
    virtual_class_devs_w=(void *)GetProcAddress(module,"SetupDiGetClassDevsW");
    virtual_class_devs_a=(void *)GetProcAddress(module,"SetupDiGetClassDevsA");
    virtual_enum_interfaces=(void *)GetProcAddress(module,"SetupDiEnumDeviceInterfaces");
}
#endif
