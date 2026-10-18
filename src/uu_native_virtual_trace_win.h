#ifndef UURB_NATIVE_VIRTUAL_TRACE_WIN_H
#define UURB_NATIVE_VIRTUAL_TRACE_WIN_H
#include <setupapi.h>
#include <cfgmgr32.h>
#include <newdev.h>
/* User-space implementation of the reviewed IDD control interface. It is
 * advertised only while the same-UID native virtual-screen service answers.
 * Every successful plug/unplug corresponds to an actual Mutter operation. */
static const GUID uurb_idd_interface = {0xc5710012,0xf569,0x4079,{0x84,0x14,0x56,0x4a,0xb6,0x32,0x83,0x47}};
static HDEVINFO (WINAPI *virtual_class_devs_w)(const GUID *, PCWSTR, HWND, DWORD);
static HDEVINFO (WINAPI *virtual_class_devs_a)(const GUID *, PCSTR, HWND, DWORD);
static HDEVINFO (WINAPI *virtual_create_device_list)(const GUID *, HWND);
static BOOL (WINAPI *virtual_create_device_w)(HDEVINFO, PCWSTR, const GUID *, PCWSTR, HWND, DWORD, PSP_DEVINFO_DATA);
static BOOL (WINAPI *virtual_enum_interfaces)(HDEVINFO, PSP_DEVINFO_DATA, const GUID *, DWORD, PSP_DEVICE_INTERFACE_DATA);
static BOOL (WINAPI *virtual_enum_device_info)(HDEVINFO, DWORD, PSP_DEVINFO_DATA);
static BOOL (WINAPI *virtual_detail_w)(HDEVINFO, PSP_DEVICE_INTERFACE_DATA, PSP_DEVICE_INTERFACE_DETAIL_DATA_W, DWORD, PDWORD, PSP_DEVINFO_DATA);
static BOOL (WINAPI *virtual_detail_a)(HDEVINFO, PSP_DEVICE_INTERFACE_DATA, PSP_DEVICE_INTERFACE_DETAIL_DATA_A, DWORD, PDWORD, PSP_DEVINFO_DATA);
static BOOL (WINAPI *virtual_get_device_registry_property_w)(HDEVINFO, PSP_DEVINFO_DATA, DWORD, PDWORD, PBYTE, DWORD, PDWORD);
static BOOL (WINAPI *virtual_call_class_installer)(DI_FUNCTION, HDEVINFO, PSP_DEVINFO_DATA);
static BOOL (WINAPI *virtual_get_device_property_w)(HDEVINFO, PSP_DEVINFO_DATA, const DEVPROPKEY *, DEVPROPTYPE *, PBYTE, DWORD, PDWORD, DWORD);
static BOOL (WINAPI *virtual_set_device_registry_property_w)(HDEVINFO, PSP_DEVINFO_DATA, DWORD, const BYTE *, DWORD);
static BOOL (WINAPI *virtual_get_device_install_params_w)(HDEVINFO, PSP_DEVINFO_DATA, PSP_DEVINSTALL_PARAMS_W);
static BOOL (WINAPI *virtual_set_class_install_params_w)(HDEVINFO, PSP_DEVINFO_DATA, PSP_CLASSINSTALL_HEADER, DWORD);
static BOOL (WINAPI *virtual_get_inf_class_w)(PCWSTR, LPGUID, PWSTR, DWORD, PUINT);
static BOOL (WINAPI *virtual_uninstall_oem_inf_w)(PCWSTR, DWORD, PVOID);
static BOOL (WINAPI *virtual_destroy)(HDEVINFO);
static CONFIGRET (WINAPI *virtual_cm_reenumerate)(DEVINST, ULONG);
static CONFIGRET (WINAPI *virtual_cm_query_remove_w)(DEVINST, PPNP_VETO_TYPE, LPWSTR, ULONG, ULONG);
static CONFIGRET (WINAPI *virtual_cm_locate_w)(PDEVINST, DEVINSTID_W, ULONG);
static CONFIGRET (WINAPI *virtual_cm_sibling)(PDEVINST, DEVINST, ULONG);
static CONFIGRET (WINAPI *virtual_cm_child)(PDEVINST, DEVINST, ULONG);
static CONFIGRET (WINAPI *virtual_cm_list_size_w)(PULONG, LPGUID, DEVINSTID_W, ULONG);
static CONFIGRET (WINAPI *virtual_cm_list_w)(LPGUID, DEVINSTID_W, PWCHAR, ULONG, ULONG);
static BOOL (WINAPI *virtual_update_driver_w)(HWND, LPCWSTR, LPCWSTR, DWORD, PBOOL);
static HANDLE (WINAPI *virtual_create_w)(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
static HANDLE (WINAPI *virtual_create_a)(LPCSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
static BOOL (WINAPI *virtual_ioctl)(HANDLE, DWORD, LPVOID, DWORD, LPVOID, DWORD, LPDWORD, LPOVERLAPPED);
static BOOL (WINAPI *virtual_close)(HANDLE);
static const WCHAR virtual_path_w[] = L"\\\\?\\uurb-native-idd";
static const char virtual_path_a[] = "\\\\?\\uurb-native-idd";
static HANDLE virtual_sets[64], virtual_handles[64];
static SRWLOCK virtual_lock = SRWLOCK_INIT;
static volatile LONG virtual_control_calls;
static volatile LONG virtual_setup_calls;

static BOOL virtual_copy(void *target, const void *source, SIZE_T bytes, BOOL write)
{
    SIZE_T count = 0;
    return (write ? WriteProcessMemory(GetCurrentProcess(), target, source, bytes, &count) :
                    ReadProcessMemory(GetCurrentProcess(), source, target, bytes, &count)) && count == bytes;
}
static BOOL virtual_track(HANDLE *table, HANDLE value, int operation)
{
    if (!value || value == INVALID_HANDLE_VALUE) return FALSE;
    BOOL result = FALSE;
    unsigned free_slot = 64;
    AcquireSRWLockExclusive(&virtual_lock);
    for (unsigned i = 0; i < 64; ++i) {
        if (table[i] == value) {
            if (operation == -1) table[i] = NULL;
            result = TRUE;
            break;
        }
        if (!table[i] && free_slot == 64) free_slot = i;
    }
    if (!result && operation == 1 && free_slot < 64) {
        table[free_slot] = value;
        result = TRUE;
    }
    ReleaseSRWLockExclusive(&virtual_lock);
    return result;
}
static BOOL virtual_available(void)
{
    return native_display_reconfigure_enabled && native_virtual_control_api &&
           !native_virtual_control_api(native_display_endpoint, 0, NULL, 0);
}
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
        snprintf(line,sizeof(line),"UURB_NATIVE_VIRTUAL {\"api\":\"%s\",\"pid\":%lu,\"flags\":%lu,\"index\":%lu,\"success\":%s,\"error\":%lu}\r\n",
            names[api],(unsigned long)GetCurrentProcessId(),(unsigned long)flags,(unsigned long)index,success?"true":"false",(unsigned long)error);
        write_log(line);flush_log();
    }
}
static void virtual_api_trace(const char *api, DWORD code, DWORD bytes, DWORD output_bytes,
                              BOOL success, DWORD error, DWORD first, DWORD second)
{
    LONG sequence = InterlockedIncrement(&virtual_control_calls);
    if (sequence <= 256) {
        char line[384];
        snprintf(line, sizeof(line),
                 "UURB_NATIVE_VIRTUAL {\"api\":\"%s\",\"pid\":%lu,\"control\":%lu,\"bytes\":%lu,\"output_bytes\":%lu,\"first\":%lu,\"second\":%lu,\"success\":%s,\"error\":%lu}\r\n",
                 api, (unsigned long)GetCurrentProcessId(), (unsigned long)code, (unsigned long)bytes, (unsigned long)output_bytes,
                 (unsigned long)first, (unsigned long)second, success ? "true" : "false",
                 (unsigned long)error);
        write_log(line); flush_log();
    }
}
static DWORD virtual_wide_hash(PCWSTR value)
{
    WCHAR copy[512]; SIZE_T bytes = 0; DWORD hash = 2166136261u;
    if (!value || !ReadProcessMemory(GetCurrentProcess(), value, copy, sizeof(copy) - sizeof(WCHAR), &bytes))
        return 0;
    copy[bytes / sizeof(WCHAR)] = 0;
    for (SIZE_T i = 0; copy[i] && i < ARRAYSIZE(copy) - 1; ++i) {
        hash ^= (DWORD)copy[i]; hash *= 16777619u;
    }
    return hash;
}
static void virtual_setup_trace(const char *api, DWORD arg1, DWORD arg2, BOOL success, DWORD error)
{
    LONG sequence = InterlockedIncrement(&virtual_setup_calls);
    if (sequence <= 512) {
        char line[384];
        snprintf(line, sizeof(line),
                 "UURB_NATIVE_VIRTUAL {\"api\":\"%s\",\"pid\":%lu,\"arg1\":%lu,\"arg2\":%lu,\"success\":%s,\"error\":%lu}\r\n",
                 api, (unsigned long)GetCurrentProcessId(), (unsigned long)arg1, (unsigned long)arg2,
                 success ? "true" : "false", (unsigned long)error);
        write_log(line); flush_log();
    }
}
static BOOL virtual_driver_name(PCWSTR value)
{
    WCHAR copy[512]; SIZE_T bytes = 0;
    if (!value || !ReadProcessMemory(GetCurrentProcess(), value, copy, sizeof(copy) - sizeof(WCHAR), &bytes)) return FALSE;
    copy[bytes / sizeof(WCHAR)] = 0;
    for (WCHAR *p = copy; *p; ++p) if (*p >= L'A' && *p <= L'Z') *p += L'a' - L'A';
    return wcsstr(copy, L"gamevieweridd") != NULL || wcsstr(copy, L"super_screen") != NULL;
}
static BOOL virtual_install_screen(void)
{
    BYTE payload[24] = {0};
    if (!native_display_reconfigure_enabled || !native_virtual_control_api) return FALSE;
    /* The vendor driver is a single adapter; slot 0 is stable for its first
     * and subsequent installations. The display service makes creation
     * idempotent, so retries after a partial setup cannot duplicate it. */
    DWORD status = native_virtual_control_api(native_display_endpoint, 0x30c004, payload, sizeof(payload));
    virtual_api_trace("virtual-install-create", 0x30c004, sizeof(payload), 0,
                      status == 0, status ? ERROR_GEN_FAILURE : ERROR_SUCCESS, 0, 0);
    if (!status) {
        AcquireSRWLockExclusive(&native_multi_lock); native_multi_time = 0; ReleaseSRWLockExclusive(&native_multi_lock);
        SendNotifyMessageW(HWND_BROADCAST, WM_DISPLAYCHANGE, 32, 0);
    }
    return status == 0;
}
static HDEVINFO WINAPI traced_virtual_class_w(const GUID *guid, PCWSTR enumerator, HWND parent, DWORD flags)
{
    DWORD saved=GetLastError(); BOOL matched=virtual_guid_matches(guid); SetLastError(saved);
    HDEVINFO result=virtual_class_devs_w(guid,enumerator,parent,flags);saved=GetLastError();
    BOOL synthetic = matched && !enumerator && (flags & DIGCF_DEVICEINTERFACE) &&
        result != INVALID_HANDLE_VALUE && virtual_available();
    if (synthetic) {
        virtual_track(virtual_sets, result, 1);
        saved = ERROR_SUCCESS;
    }
    if (matched) virtual_discovery_trace(0,flags,0,result!=INVALID_HANDLE_VALUE,saved);
    SetLastError(saved);return result;
}
static HDEVINFO WINAPI traced_virtual_class_a(const GUID *guid, PCSTR enumerator, HWND parent, DWORD flags)
{
    DWORD saved=GetLastError(); BOOL matched=virtual_guid_matches(guid); SetLastError(saved);
    HDEVINFO result=virtual_class_devs_a(guid,enumerator,parent,flags);saved=GetLastError();
    BOOL synthetic = matched && !enumerator && (flags & DIGCF_DEVICEINTERFACE) &&
        result != INVALID_HANDLE_VALUE && virtual_available();
    if (synthetic) {
        virtual_track(virtual_sets, result, 1);
        saved = ERROR_SUCCESS;
    }
    if (matched) virtual_discovery_trace(1,flags,0,result!=INVALID_HANDLE_VALUE,saved);
    SetLastError(saved);return result;
}
static HDEVINFO WINAPI traced_virtual_create_list(const GUID *guid, HWND parent)
{
    DWORD saved = GetLastError(); BOOL matched = virtual_guid_matches(guid);
    HDEVINFO result = virtual_create_device_list(guid, parent); saved = GetLastError();
    if (matched && result != INVALID_HANDLE_VALUE && virtual_available()) {
        virtual_track(virtual_sets, result, 1); saved = ERROR_SUCCESS;
    }
    virtual_setup_trace("SetupDiCreateDeviceInfoList", matched ? 1 : 0,
                        result == INVALID_HANDLE_VALUE ? 0 : 1, result != INVALID_HANDLE_VALUE, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_create_device(HDEVINFO set, PCWSTR name, const GUID *guid,
                                                PCWSTR description, HWND parent, DWORD flags,
                                                PSP_DEVINFO_DATA output)
{
    DWORD saved = GetLastError(); BOOL matched = virtual_guid_matches(guid);
    BOOL vendor = virtual_driver_name(name) || virtual_driver_name(description);
    BOOL result = virtual_create_device_w(set, name, guid, description, parent, flags, output);
    saved = GetLastError();
    if (vendor && virtual_available()) {
        virtual_track(virtual_sets, set, 1);
        if (!result && virtual_install_screen()) {
            if (output) {
                SP_DEVINFO_DATA value = {0}; value.cbSize = sizeof(value);
                value.ClassGuid = uurb_idd_interface;
                if (!virtual_copy(output, &value, sizeof(value), TRUE)) {
                    SetLastError(ERROR_INVALID_USER_BUFFER); result = FALSE; saved = ERROR_INVALID_USER_BUFFER;
                } else { result = TRUE; saved = ERROR_SUCCESS; }
            } else { result = TRUE; saved = ERROR_SUCCESS; }
        }
    }
    virtual_setup_trace("SetupDiCreateDeviceInfoW", matched ? 1 : 0, flags, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_enum_device_info(HDEVINFO set, DWORD index, PSP_DEVINFO_DATA output)
{
    BOOL tracked = virtual_track(virtual_sets, set, 0);
    if (tracked) {
        if (index) {
            SetLastError(ERROR_NO_MORE_ITEMS);
            virtual_setup_trace("SetupDiEnumDeviceInfo", 1, index, FALSE, ERROR_NO_MORE_ITEMS);
            return FALSE;
        }
        SP_DEVINFO_DATA value = {0};
        value.cbSize = sizeof(value);
        value.ClassGuid = uurb_idd_interface;
        value.DevInst = 1;
        BOOL copied = output && virtual_copy(output, &value, sizeof(value), TRUE);
        SetLastError(copied ? ERROR_SUCCESS : ERROR_INVALID_USER_BUFFER);
        virtual_setup_trace("SetupDiEnumDeviceInfo", 1, index, copied,
                            copied ? ERROR_SUCCESS : ERROR_INVALID_USER_BUFFER);
        return copied;
    }
    BOOL result = virtual_enum_device_info(set, index, output); DWORD saved = GetLastError();
    virtual_setup_trace("SetupDiEnumDeviceInfo", tracked ? 1 : 0, index, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_call_class_installer(DI_FUNCTION function, HDEVINFO set,
                                                       PSP_DEVINFO_DATA device)
{
    BOOL tracked = virtual_track(virtual_sets, set, 0);
    BOOL result;
    if (tracked && virtual_available()) {
        if (function == DIF_INSTALLDEVICE) {
            result = virtual_install_screen();
        } else if (function == DIF_REMOVE || function == (DI_FUNCTION)0x1d /* DIF_UNINSTALLDEVICE */) {
            BYTE slot[4] = {0};
            DWORD status = native_virtual_control_api(native_display_endpoint, 0x30c008, slot, sizeof(slot));
            result = status == 0;
        } else {
            /* REGISTERDEVICE and the class-install bookkeeping calls have no
             * kernel equivalent in Wine.  The virtual endpoint already owns
             * the actual Mutter monitor, so report the bookkeeping step as
             * completed. */
            result = TRUE;
        }
        SetLastError(result ? ERROR_SUCCESS : ERROR_GEN_FAILURE);
    } else result = virtual_call_class_installer(function, set, device);
    DWORD saved = GetLastError();
    virtual_setup_trace("SetupDiCallClassInstaller", function, tracked ? 1 : 0, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_get_device_registry_property_w(HDEVINFO set, PSP_DEVINFO_DATA device,
                                                                  DWORD property, PDWORD type, PBYTE buffer,
                                                                  DWORD bytes, PDWORD required)
{
    BOOL tracked = virtual_track(virtual_sets, set, 0);
    if (!tracked) {
        BOOL result = virtual_get_device_registry_property_w(set, device, property, type, buffer, bytes, required);
        DWORD saved = GetLastError();
        virtual_setup_trace("SetupDiGetDeviceRegistryPropertyW", property, bytes, result, saved);
        SetLastError(saved); return result;
    }
    static const WCHAR friendly[] = L"GameViewer Virtual Display Adapter";
    static const WCHAR hardware[] = L"ROOT\\GameViewerIddDriver\0";
    static const WCHAR class_guid[] = L"{C5710012-F569-4079-8414-564AB6328347}";
    const WCHAR *value = property == SPDRP_HARDWAREID ? hardware :
                         property == SPDRP_CLASSGUID ? class_guid : friendly;
    DWORD value_type = property == SPDRP_HARDWAREID ? REG_MULTI_SZ : REG_SZ;
    DWORD needed = (DWORD)((wcslen(value) + (value_type == REG_MULTI_SZ ? 2 : 1)) * sizeof(WCHAR));
    if (required) virtual_copy(required, &needed, sizeof(needed), TRUE);
    if (type) virtual_copy(type, &value_type, sizeof(value_type), TRUE);
    BOOL result = buffer && bytes >= needed && virtual_copy(buffer, value, needed, TRUE);
    DWORD saved = result ? ERROR_SUCCESS : ERROR_INSUFFICIENT_BUFFER;
    virtual_setup_trace("SetupDiGetDeviceRegistryPropertyW", property, bytes, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_get_device_property_w(HDEVINFO set, PSP_DEVINFO_DATA device,
                                                        const DEVPROPKEY *key, DEVPROPTYPE *type,
                                                        PBYTE buffer, DWORD bytes, PDWORD required, DWORD flags)
{
    BOOL tracked = virtual_track(virtual_sets, set, 0);
    BOOL result = virtual_get_device_property_w(set, device, key, type, buffer, bytes, required, flags);
    DWORD saved = GetLastError(); virtual_setup_trace("SetupDiGetDevicePropertyW", tracked ? 1 : 0, bytes, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_set_device_registry_property_w(HDEVINFO set, PSP_DEVINFO_DATA device,
                                                                  DWORD property, const BYTE *buffer, DWORD bytes)
{
    BOOL tracked = virtual_track(virtual_sets, set, 0);
    BOOL result = tracked ? TRUE : virtual_set_device_registry_property_w(set, device, property, buffer, bytes);
    DWORD saved = tracked ? ERROR_SUCCESS : GetLastError();
    virtual_setup_trace("SetupDiSetDeviceRegistryPropertyW", property, tracked ? bytes : 0, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_get_device_install_params_w(HDEVINFO set, PSP_DEVINFO_DATA device,
                                                               PSP_DEVINSTALL_PARAMS_W params)
{
    BOOL tracked = virtual_track(virtual_sets, set, 0);
    BOOL result;
    if (tracked) {
        SP_DEVINSTALL_PARAMS_W value = {0};
        value.cbSize = sizeof(value);
        result = params && virtual_copy(params, &value, sizeof(value), TRUE);
        SetLastError(result ? ERROR_SUCCESS : ERROR_INVALID_USER_BUFFER);
    } else result = virtual_get_device_install_params_w(set, device, params);
    DWORD saved = tracked ? (result ? ERROR_SUCCESS : ERROR_INVALID_USER_BUFFER) : GetLastError();
    virtual_setup_trace("SetupDiGetDeviceInstallParamsW", tracked ? 1 : 0, 0, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_set_class_install_params_w(HDEVINFO set, PSP_DEVINFO_DATA device,
                                                              PSP_CLASSINSTALL_HEADER params, DWORD bytes)
{
    BOOL tracked = virtual_track(virtual_sets, set, 0);
    BOOL result = tracked ? TRUE : virtual_set_class_install_params_w(set, device, params, bytes);
    DWORD saved = tracked ? ERROR_SUCCESS : GetLastError();
    virtual_setup_trace("SetupDiSetClassInstallParamsW", tracked ? 1 : 0, bytes, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_get_inf_class_w(PCWSTR path, LPGUID guid, PWSTR name, DWORD bytes, PUINT required)
{
    BOOL result = virtual_get_inf_class_w(path, guid, name, bytes, required); DWORD saved = GetLastError();
    virtual_setup_trace("SetupDiGetINFClassW", virtual_wide_hash(path), bytes, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_uninstall_oem_inf_w(PCWSTR name, DWORD flags, PVOID reserved)
{
    BOOL result;
    if (virtual_driver_name(name) && virtual_available()) {
        /* Wine cannot own a kernel/UMDF package. Treat removal of the vendor
         * package as an idempotent lifecycle step; the actual output is
         * removed through the reviewed virtual-control path. */
        result = TRUE; SetLastError(ERROR_SUCCESS);
    } else result = virtual_uninstall_oem_inf_w(name, flags, reserved);
    DWORD saved = GetLastError();
    virtual_setup_trace("SetupUninstallOEMInfW", virtual_wide_hash(name), flags, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_update_driver_w(HWND parent, LPCWSTR hardware_id, LPCWSTR inf_path,
                                                  DWORD flags, PBOOL reboot)
{
    BOOL vendor = virtual_driver_name(hardware_id) || virtual_driver_name(inf_path);
    if (vendor && virtual_available()) {
        BOOL result = virtual_install_screen();
        if (reboot) virtual_copy(reboot, &(BOOL){FALSE}, sizeof(BOOL), TRUE);
        SetLastError(result ? ERROR_SUCCESS : ERROR_GEN_FAILURE);
        virtual_setup_trace("UpdateDriverForPlugAndPlayDevicesW", virtual_wide_hash(hardware_id),
                            virtual_wide_hash(inf_path), result, result ? ERROR_SUCCESS : ERROR_GEN_FAILURE);
        return result;
    }
    BOOL result = virtual_update_driver_w(parent, hardware_id, inf_path, flags, reboot); DWORD saved = GetLastError();
    virtual_setup_trace("UpdateDriverForPlugAndPlayDevicesW", virtual_wide_hash(hardware_id),
                        virtual_wide_hash(inf_path), result, saved);
    SetLastError(saved); return result;
}
static CONFIGRET WINAPI traced_virtual_cm_reenumerate(DEVINST node, ULONG flags)
{
    CONFIGRET result = virtual_cm_reenumerate(node, flags);
    virtual_setup_trace("CM_Reenumerate_DevNode", (DWORD)node, flags, result == CR_SUCCESS, result);
    return result;
}
static CONFIGRET WINAPI traced_virtual_cm_query_remove_w(DEVINST node, PPNP_VETO_TYPE veto,
                                                         LPWSTR name, ULONG length, ULONG flags)
{
    CONFIGRET result = virtual_cm_query_remove_w(node, veto, name, length, flags);
    virtual_setup_trace("CM_Query_And_Remove_SubTreeW", (DWORD)node, flags, result == CR_SUCCESS, result);
    return result;
}
static CONFIGRET WINAPI traced_virtual_cm_locate_w(PDEVINST output, DEVINSTID_W id, ULONG flags)
{
    CONFIGRET result = virtual_cm_locate_w(output, id, flags);
    virtual_setup_trace("CM_Locate_DevNodeW", virtual_wide_hash(id), flags, result == CR_SUCCESS, result);
    return result;
}
static CONFIGRET WINAPI traced_virtual_cm_sibling(PDEVINST output, DEVINST node, ULONG flags)
{
    CONFIGRET result = virtual_cm_sibling(output, node, flags);
    virtual_setup_trace("CM_Get_Sibling", (DWORD)node, flags, result == CR_SUCCESS, result);
    return result;
}
static CONFIGRET WINAPI traced_virtual_cm_child(PDEVINST output, DEVINST node, ULONG flags)
{
    CONFIGRET result = virtual_cm_child(output, node, flags);
    virtual_setup_trace("CM_Get_Child", (DWORD)node, flags, result == CR_SUCCESS, result);
    return result;
}
static CONFIGRET WINAPI traced_virtual_cm_list_size_w(PULONG length, LPGUID guid, DEVINSTID_W id, ULONG flags)
{
    CONFIGRET result = virtual_cm_list_size_w(length, guid, id, flags);
    virtual_setup_trace("CM_Get_Device_Interface_List_SizeW", flags, length ? *length : 0,
                        result == CR_SUCCESS, result);
    return result;
}
static CONFIGRET WINAPI traced_virtual_cm_list_w(LPGUID guid, DEVINSTID_W id, PWCHAR buffer,
                                                 ULONG length, ULONG flags)
{
    CONFIGRET result = virtual_cm_list_w(guid, id, buffer, length, flags);
    virtual_setup_trace("CM_Get_Device_Interface_ListW", length, flags, result == CR_SUCCESS, result);
    return result;
}
static BOOL WINAPI traced_virtual_enum(HDEVINFO set, PSP_DEVINFO_DATA device, const GUID *guid,
                                       DWORD index, PSP_DEVICE_INTERFACE_DATA output)
{
    DWORD saved=GetLastError(); BOOL matched=virtual_guid_matches(guid); SetLastError(saved);
    BOOL result;
    if (matched && !device && virtual_track(virtual_sets, set, 0)) {
        SP_DEVICE_INTERFACE_DATA value;
        if (!output || !virtual_copy(&value, output, sizeof(value), FALSE) || value.cbSize != sizeof(value)) {
            SetLastError(ERROR_INVALID_USER_BUFFER); return FALSE;
        }
        if (index) { SetLastError(ERROR_NO_MORE_ITEMS); return FALSE; }
        ZeroMemory(&value, sizeof(value)); value.cbSize = sizeof(value);
        value.InterfaceClassGuid = uurb_idd_interface; value.Flags = SPINT_ACTIVE;
        result = virtual_copy(output, &value, sizeof(value), TRUE);
        SetLastError(result ? ERROR_SUCCESS : ERROR_INVALID_USER_BUFFER);
    } else result=virtual_enum_interfaces(set,device,guid,index,output);
    saved=GetLastError();
    if (matched) virtual_discovery_trace(2,0,index,result,saved);
    SetLastError(saved);return result;
}
static BOOL virtual_detail(HDEVINFO set, PSP_DEVICE_INTERFACE_DATA data, void *output,
                           DWORD capacity, PDWORD required, PSP_DEVINFO_DATA device, BOOL wide)
{
    (void)set;
    SP_DEVICE_INTERFACE_DATA interface;
    if (!data || !virtual_copy(&interface, data, sizeof(interface), FALSE) ||
        interface.cbSize != sizeof(interface) || !virtual_guid_matches(&interface.InterfaceClassGuid)) {
        SetLastError(ERROR_INVALID_PARAMETER); return FALSE;
    }
    DWORD bytes = (DWORD)(offsetof(SP_DEVICE_INTERFACE_DETAIL_DATA_W, DevicePath) +
                         (wide ? sizeof(virtual_path_w) : sizeof(virtual_path_a)));
    if (required && !virtual_copy(required, &bytes, sizeof(bytes), TRUE)) {
        SetLastError(ERROR_INVALID_USER_BUFFER); return FALSE;
    }
    if (!output || capacity < bytes) { SetLastError(ERROR_INSUFFICIENT_BUFFER); return FALSE; }
    DWORD cb;
    if (!virtual_copy(&cb, output, sizeof(cb), FALSE) || cb != (wide ? sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) :
                                                                 sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_A))) {
        SetLastError(ERROR_INVALID_USER_BUFFER); return FALSE;
    }
    if (device) {
        SP_DEVINFO_DATA info;
        if (!virtual_copy(&info, device, sizeof(info), FALSE) || info.cbSize != sizeof(info)) {
            SetLastError(ERROR_INVALID_USER_BUFFER); return FALSE;
        }
        ZeroMemory(&info, sizeof(info)); info.cbSize = sizeof(info); info.ClassGuid = uurb_idd_interface;
        if (!virtual_copy(device, &info, sizeof(info), TRUE)) {
            SetLastError(ERROR_INVALID_USER_BUFFER); return FALSE;
        }
    }
    if (!virtual_copy((BYTE *)output + offsetof(SP_DEVICE_INTERFACE_DETAIL_DATA_W, DevicePath),
                      wide ? (const void *)virtual_path_w : (const void *)virtual_path_a,
                      wide ? sizeof(virtual_path_w) : sizeof(virtual_path_a), TRUE)) {
        SetLastError(ERROR_INVALID_USER_BUFFER); return FALSE;
    }
    SetLastError(ERROR_SUCCESS); return TRUE;
}
static BOOL WINAPI traced_virtual_detail_w(HDEVINFO set, PSP_DEVICE_INTERFACE_DATA data,
    PSP_DEVICE_INTERFACE_DETAIL_DATA_W output, DWORD bytes, PDWORD required, PSP_DEVINFO_DATA device)
{
    BOOL synthetic = virtual_track(virtual_sets, set, 0);
    BOOL result = synthetic ? virtual_detail(set, data, output, bytes, required, device, TRUE) :
        virtual_detail_w(set, data, output, bytes, required, device);
    virtual_api_trace("SetupDiGetDeviceInterfaceDetailW", 0, bytes, 0, result, result ? ERROR_SUCCESS : GetLastError(), 0, 0);
    return result;
}
static BOOL WINAPI traced_virtual_detail_a(HDEVINFO set, PSP_DEVICE_INTERFACE_DATA data,
    PSP_DEVICE_INTERFACE_DETAIL_DATA_A output, DWORD bytes, PDWORD required, PSP_DEVINFO_DATA device)
{
    BOOL synthetic = virtual_track(virtual_sets, set, 0);
    BOOL result = synthetic ? virtual_detail(set, data, output, bytes, required, device, FALSE) :
        virtual_detail_a(set, data, output, bytes, required, device);
    virtual_api_trace("SetupDiGetDeviceInterfaceDetailA", 0, bytes, 0, result, result ? ERROR_SUCCESS : GetLastError(), 0, 0);
    return result;
}
static BOOL WINAPI traced_virtual_destroy(HDEVINFO set)
{
    virtual_track(virtual_sets, set, -1);
    return virtual_destroy(set);
}
static HANDLE virtual_open(void)
{
    if (!virtual_available()) { SetLastError(ERROR_NOT_READY); return INVALID_HANDLE_VALUE; }
    HANDLE handle = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (handle && virtual_track(virtual_handles, handle, 1)) {
        virtual_api_trace("CreateFile", 0, 0, 0, TRUE, ERROR_SUCCESS, 0, 0);
        return handle;
    }
    if (handle) virtual_close(handle);
    SetLastError(ERROR_TOO_MANY_OPEN_FILES); return INVALID_HANDLE_VALUE;
}
static HANDLE WINAPI traced_virtual_create_w(LPCWSTR name, DWORD access, DWORD sharing,
    LPSECURITY_ATTRIBUTES attributes, DWORD disposition, DWORD flags, HANDLE template)
{
    WCHAR copied[ARRAYSIZE(virtual_path_w)];
    if (name && virtual_copy(copied, name, sizeof(copied), FALSE) &&
        !memcmp(copied, virtual_path_w, sizeof(copied))) return virtual_open();
    HANDLE result = virtual_create_w(name, access, sharing, attributes, disposition, flags, template);
    DWORD saved = GetLastError();
    virtual_setup_trace("CreateFileW", virtual_wide_hash(name), access, result != INVALID_HANDLE_VALUE, saved);
    SetLastError(saved); return result;
}
static HANDLE WINAPI traced_virtual_create_a(LPCSTR name, DWORD access, DWORD sharing,
    LPSECURITY_ATTRIBUTES attributes, DWORD disposition, DWORD flags, HANDLE template)
{
    char copied[sizeof(virtual_path_a)];
    if (name && virtual_copy(copied, name, sizeof(copied), FALSE) &&
        !memcmp(copied, virtual_path_a, sizeof(copied))) return virtual_open();
    HANDLE result = virtual_create_a(name, access, sharing, attributes, disposition, flags, template);
    DWORD saved = GetLastError();
    virtual_setup_trace("CreateFileA", name ? (DWORD)strlen(name) : 0, access, result != INVALID_HANDLE_VALUE, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_close(HANDLE handle)
{
    virtual_track(virtual_handles, handle, -1);
    BOOL result = virtual_close(handle); DWORD saved = GetLastError();
    virtual_setup_trace("CloseHandle", 0, 0, result, saved);
    SetLastError(saved); return result;
}
static BOOL WINAPI traced_virtual_ioctl(HANDLE handle, DWORD code, LPVOID input, DWORD bytes,
    LPVOID output, DWORD output_bytes, LPDWORD returned, LPOVERLAPPED overlapped)
{
    if (!virtual_track(virtual_handles, handle, 0)) {
        BOOL result = virtual_ioctl(handle, code, input, bytes, output, output_bytes, returned, overlapped);
        DWORD saved = GetLastError(); DWORD first = 0, second = 0;
        if (input && bytes >= sizeof(first)) virtual_copy(&first, input, sizeof(first), FALSE);
        if (input && bytes >= sizeof(first) + sizeof(second)) virtual_copy(&second,
            (const BYTE *)input + sizeof(first), sizeof(second), FALSE);
        virtual_api_trace("DeviceIoControl-passthrough", code, bytes, output_bytes, result, saved, first, second);
        SetLastError(saved); return result;
    }
    BYTE payload[1024]; DWORD zero = 0;
    DWORD first = 0, second = 0;
    if (input && bytes >= sizeof(first)) virtual_copy(&first, input, sizeof(first), FALSE);
    if (input && bytes >= sizeof(first) + sizeof(second)) virtual_copy(&second,
        (const BYTE *)input + sizeof(first), sizeof(second), FALSE);
    if (overlapped || bytes > sizeof(payload) || output_bytes > sizeof(payload) || !returned ||
        !virtual_copy(returned, &zero, sizeof(zero), TRUE) ||
        (bytes && (!input || !virtual_copy(payload, input, bytes, FALSE))) ||
        (output_bytes && !output)) {
        SetLastError(ERROR_INVALID_PARAMETER);
        virtual_api_trace("DeviceIoControl", code, bytes, output_bytes, FALSE,
                          ERROR_INVALID_PARAMETER, first, second);
        return FALSE;
    }
    if (!native_display_reconfigure_enabled || !native_virtual_control_api) {
        SetLastError(ERROR_ACCESS_DENIED);
        virtual_api_trace("DeviceIoControl", code, bytes, output_bytes, FALSE,
                          ERROR_ACCESS_DENIED, first, second);
        return FALSE;
    }
    DWORD status = native_virtual_control_api(native_display_endpoint, code, payload, bytes);
    if (output_bytes) {
        BYTE cleared[1024] = {0};
        if (!virtual_copy(output, cleared, output_bytes, TRUE)) {
            SetLastError(ERROR_INVALID_USER_BUFFER);
            virtual_api_trace("DeviceIoControl", code, bytes, output_bytes, FALSE,
                              ERROR_INVALID_USER_BUFFER, first, second);
            return FALSE;
        }
    }
    virtual_api_trace("DeviceIoControl", code, bytes, output_bytes, status == 0,
                      status ? ERROR_GEN_FAILURE : ERROR_SUCCESS, first, second);
    if (!status) {
        AcquireSRWLockExclusive(&native_multi_lock); native_multi_time = 0; ReleaseSRWLockExclusive(&native_multi_lock);
        if (code == 0x30c004 || code == 0x30c008 || code == 0x30c00c)
            SendNotifyMessageW(HWND_BROADCAST, WM_DISPLAYCHANGE, 32, 0);
    }
    SetLastError(status ? ERROR_GEN_FAILURE : ERROR_SUCCESS);
    return status == 0;
}
static void *native_virtual_hook_for(uintptr_t original)
{
    if (!original) return NULL;
    /* newdev.dll and cfgmgr32.dll may be delay-loaded after the bridge's
     * worker thread starts. Refresh their exports before comparing an IAT or
     * GetProcAddress result, otherwise the install path silently passes
     * through while enumeration remains instrumented. */
    HMODULE module = GetModuleHandleW(L"newdev.dll");
    if (module && !virtual_update_driver_w)
        virtual_update_driver_w = (void *)GetProcAddress(module, "UpdateDriverForPlugAndPlayDevicesW");
    module = GetModuleHandleW(L"setupapi.dll");
    if (module && !virtual_create_device_list)
        virtual_create_device_list = (void *)GetProcAddress(module, "SetupDiCreateDeviceInfoList");
    if (original==(uintptr_t)virtual_class_devs_w) return (void *)&traced_virtual_class_w;
    if (original==(uintptr_t)virtual_class_devs_a) return (void *)&traced_virtual_class_a;
    if (original==(uintptr_t)virtual_create_device_list) return (void *)&traced_virtual_create_list;
    if (original==(uintptr_t)virtual_create_device_w) return (void *)&traced_virtual_create_device;
    if (original==(uintptr_t)virtual_enum_interfaces) return (void *)&traced_virtual_enum;
    if (original==(uintptr_t)virtual_enum_device_info) return (void *)&traced_virtual_enum_device_info;
    if (original==(uintptr_t)virtual_detail_w) return (void *)&traced_virtual_detail_w;
    if (original==(uintptr_t)virtual_detail_a) return (void *)&traced_virtual_detail_a;
    if (original==(uintptr_t)virtual_call_class_installer) return (void *)&traced_virtual_call_class_installer;
    if (original==(uintptr_t)virtual_get_device_registry_property_w) return (void *)&traced_virtual_get_device_registry_property_w;
    if (original==(uintptr_t)virtual_get_device_property_w) return (void *)&traced_virtual_get_device_property_w;
    if (original==(uintptr_t)virtual_set_device_registry_property_w) return (void *)&traced_virtual_set_device_registry_property_w;
    if (original==(uintptr_t)virtual_get_device_install_params_w) return (void *)&traced_virtual_get_device_install_params_w;
    if (original==(uintptr_t)virtual_set_class_install_params_w) return (void *)&traced_virtual_set_class_install_params_w;
    if (original==(uintptr_t)virtual_get_inf_class_w) return (void *)&traced_virtual_get_inf_class_w;
    if (original==(uintptr_t)virtual_uninstall_oem_inf_w) return (void *)&traced_virtual_uninstall_oem_inf_w;
    if (original==(uintptr_t)virtual_update_driver_w) return (void *)&traced_virtual_update_driver_w;
    if (original==(uintptr_t)virtual_cm_reenumerate) return (void *)&traced_virtual_cm_reenumerate;
    if (original==(uintptr_t)virtual_cm_query_remove_w) return (void *)&traced_virtual_cm_query_remove_w;
    if (original==(uintptr_t)virtual_cm_locate_w) return (void *)&traced_virtual_cm_locate_w;
    if (original==(uintptr_t)virtual_cm_sibling) return (void *)&traced_virtual_cm_sibling;
    if (original==(uintptr_t)virtual_cm_child) return (void *)&traced_virtual_cm_child;
    if (original==(uintptr_t)virtual_cm_list_size_w) return (void *)&traced_virtual_cm_list_size_w;
    if (original==(uintptr_t)virtual_cm_list_w) return (void *)&traced_virtual_cm_list_w;
    if (original==(uintptr_t)virtual_destroy) return (void *)&traced_virtual_destroy;
    if (original==(uintptr_t)virtual_create_w) return (void *)&traced_virtual_create_w;
    if (original==(uintptr_t)virtual_create_a) return (void *)&traced_virtual_create_a;
    if (original==(uintptr_t)virtual_ioctl) return (void *)&traced_virtual_ioctl;
    if (original==(uintptr_t)virtual_close) return (void *)&traced_virtual_close;
    return NULL;
}
static void native_virtual_trace_initialize(void)
{
    HMODULE module=GetModuleHandleW(L"setupapi.dll");
    if (!module) return; /* Do not load a driver/library just for diagnostics. */
    virtual_class_devs_w=(void *)GetProcAddress(module,"SetupDiGetClassDevsW");
    virtual_class_devs_a=(void *)GetProcAddress(module,"SetupDiGetClassDevsA");
    virtual_create_device_list=(void *)GetProcAddress(module,"SetupDiCreateDeviceInfoList");
    virtual_create_device_w=(void *)GetProcAddress(module,"SetupDiCreateDeviceInfoW");
    virtual_enum_interfaces=(void *)GetProcAddress(module,"SetupDiEnumDeviceInterfaces");
    virtual_enum_device_info=(void *)GetProcAddress(module,"SetupDiEnumDeviceInfo");
    virtual_detail_w=(void *)GetProcAddress(module,"SetupDiGetDeviceInterfaceDetailW");
    virtual_detail_a=(void *)GetProcAddress(module,"SetupDiGetDeviceInterfaceDetailA");
    virtual_call_class_installer=(void *)GetProcAddress(module,"SetupDiCallClassInstaller");
    virtual_get_device_registry_property_w=(void *)GetProcAddress(module,"SetupDiGetDeviceRegistryPropertyW");
    virtual_get_device_property_w=(void *)GetProcAddress(module,"SetupDiGetDevicePropertyW");
    virtual_set_device_registry_property_w=(void *)GetProcAddress(module,"SetupDiSetDeviceRegistryPropertyW");
    virtual_get_device_install_params_w=(void *)GetProcAddress(module,"SetupDiGetDeviceInstallParamsW");
    virtual_set_class_install_params_w=(void *)GetProcAddress(module,"SetupDiSetClassInstallParamsW");
    virtual_get_inf_class_w=(void *)GetProcAddress(module,"SetupDiGetINFClassW");
    virtual_uninstall_oem_inf_w=(void *)GetProcAddress(module,"SetupUninstallOEMInfW");
    virtual_cm_reenumerate=(void *)GetProcAddress(module,"CM_Reenumerate_DevNode");
    virtual_cm_query_remove_w=(void *)GetProcAddress(module,"CM_Query_And_Remove_SubTreeW");
    virtual_cm_locate_w=(void *)GetProcAddress(module,"CM_Locate_DevNodeW");
    virtual_cm_sibling=(void *)GetProcAddress(module,"CM_Get_Sibling");
    virtual_cm_child=(void *)GetProcAddress(module,"CM_Get_Child");
    virtual_cm_list_size_w=(void *)GetProcAddress(module,"CM_Get_Device_Interface_List_SizeW");
    virtual_cm_list_w=(void *)GetProcAddress(module,"CM_Get_Device_Interface_ListW");
    virtual_destroy=(void *)GetProcAddress(module,"SetupDiDestroyDeviceInfoList");
    module=GetModuleHandleW(L"newdev.dll");
    if (module) virtual_update_driver_w=(void *)GetProcAddress(module,"UpdateDriverForPlugAndPlayDevicesW");
    module = GetModuleHandleW(L"kernel32.dll");
    virtual_create_w=(void *)GetProcAddress(module,"CreateFileW");
    virtual_create_a=(void *)GetProcAddress(module,"CreateFileA");
    virtual_ioctl=(void *)GetProcAddress(module,"DeviceIoControl");
    virtual_close=(void *)GetProcAddress(module,"CloseHandle");

}
#endif
