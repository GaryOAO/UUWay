#ifndef UURB_NATIVE_DISPLAY_GEOMETRY_WIN_H
#define UURB_NATIVE_DISPLAY_GEOMETRY_WIN_H
/* One native primary display, using Wine's existing adapter/monitor handles.
 * Geometry is the captured pixel extent, not a fabricated second screen.
 * Included after the native display cache/adapter declarations. */
static BOOL (WINAPI *geometry_monitor_w)(HMONITOR,LPMONITORINFO);
static BOOL (WINAPI *geometry_monitor_a)(HMONITOR,LPMONITORINFO);
static BOOL (WINAPI *geometry_monitors)(HDC,LPCRECT,MONITORENUMPROC,LPARAM);
static int (WINAPI *geometry_metrics)(int);
static LONG (WINAPI *geometry_device_info)(DISPLAYCONFIG_DEVICE_INFO_HEADER *);
static LONG (WINAPI *geometry_device_set_info)(DISPLAYCONFIG_DEVICE_INFO_HEADER *);

/* Windows' source-DPI packets are not present in older Wine headers, but the
 * official UU client uses the observed relative-scale layout: -3=query and
 * -4=set, both keyed by the source adapter LUID/id. Keep these layouts local
 * and gate them on exact packet sizes; never pass the private packets through
 * to a Wine implementation that does not understand them. */
struct uurb_source_dpi_get {
    DISPLAYCONFIG_DEVICE_INFO_HEADER header;
    int32_t min_scale_rel, cur_scale_rel, max_scale_rel;
};
struct uurb_source_dpi_set {
    DISPLAYCONFIG_DEVICE_INFO_HEADER header;
    int32_t scale_rel;
};
_Static_assert(sizeof(struct uurb_source_dpi_get) == 32, "source DPI get ABI");
_Static_assert(sizeof(struct uurb_source_dpi_set) == 24, "source DPI set ABI");
static BOOL native_display_refresh(BOOL force);
static BOOL uurb_dpi_source_is_primary(const DISPLAYCONFIG_DEVICE_INFO_HEADER *header)
{
    if (!geometry_device_info) return FALSE;
    DISPLAYCONFIG_SOURCE_DEVICE_NAME name={0};
    name.header=*header; name.header.type=DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
    name.header.size=sizeof(name);
    if (geometry_device_info(&name.header) != ERROR_SUCCESS || !display_primary(name.viewGdiDeviceName)) return FALSE;
    return TRUE;
}
static LONG WINAPI native_geometry_device_info_common(DISPLAYCONFIG_DEVICE_INFO_HEADER *packet, BOOL is_set)
{
    LONG (WINAPI *original)(DISPLAYCONFIG_DEVICE_INFO_HEADER *) = is_set ? geometry_device_set_info : geometry_device_info;
    /* Trace-only/native-input-only installations have no display endpoint.
     * Preserve the original API even for private packets and invalid pointers. */
    if (!native_display_adapter_enabled) return original ? original(packet) : ERROR_NOT_SUPPORTED;
    if (!packet) return ERROR_INVALID_PARAMETER;
    DISPLAYCONFIG_DEVICE_INFO_HEADER header;
    SIZE_T bytes=0;
    if (!ReadProcessMemory(GetCurrentProcess(),packet,&header,sizeof(header),&bytes) || bytes!=sizeof(header))
        return ERROR_INVALID_PARAMETER;
    int32_t type=(int32_t)header.type;
    if (type!=-3 && type!=-4) return original ? original(packet) : ERROR_NOT_SUPPORTED;
    if (is_set ? type != -4 : type != -3) return ERROR_NOT_SUPPORTED;
    if (is_set && !native_display_reconfigure_enabled) return ERROR_NOT_SUPPORTED;
    const SIZE_T expected=type==-3 ? sizeof(struct uurb_source_dpi_get) : sizeof(struct uurb_source_dpi_set);
    if (header.size!=expected || !uurb_dpi_source_is_primary(&header)) return ERROR_NOT_SUPPORTED;
    AcquireSRWLockExclusive(&native_display_cache_lock);
    BOOL refreshed=native_display_refresh(TRUE);
    struct uurb_display_snapshot snapshot=native_display_cache;
    ReleaseSRWLockExclusive(&native_display_cache_lock);
    if (!refreshed || !snapshot.scale_mask || (snapshot.scale_mask & ~UURB_DISPLAY_SCALE_MASK))
        return ERROR_NOT_SUPPORTED;
    int current=uurb_display_scale_index(snapshot.current.scale_milli);
    if (current<0 || !(snapshot.scale_mask & (1u << current))) return ERROR_NOT_SUPPORTED;
    /* This bridge's relative-index reference is explicitly 100%, not an
     * invented monitor/EDID recommendation. All SET values remain exact. */
    const int recommended=0;
    if (type==-3) {
        unsigned minimum=0, maximum=UURB_DISPLAY_SCALE_COUNT-1;
        while (!(snapshot.scale_mask & (1u << minimum))) ++minimum;
        while (!(snapshot.scale_mask & (1u << maximum))) --maximum;
        uint32_t interval=((1u << (maximum-minimum+1))-1u) << minimum;
        /* Windows only has min/current/max, not an arbitrary scale list.
         * [100,200] must never become [100,125,150,175,200]. Retain exact
         * host-side SET support while refusing an unrepresentable GET. */
        if (snapshot.scale_mask != interval || minimum != 0) return ERROR_NOT_SUPPORTED;
        struct uurb_source_dpi_get value={0};
        if (!ReadProcessMemory(GetCurrentProcess(),packet,&value,sizeof(value),&bytes) || bytes!=sizeof(value)) return ERROR_INVALID_PARAMETER;
        value.min_scale_rel=(int)minimum-recommended;
        value.cur_scale_rel=current-recommended;
        value.max_scale_rel=(int)maximum-recommended;
        if (!WriteProcessMemory(GetCurrentProcess(),packet,&value,sizeof(value),&bytes) || bytes!=sizeof(value)) return ERROR_INVALID_PARAMETER;
        return ERROR_SUCCESS;
    }
    struct uurb_source_dpi_set value={0};
    if (!ReadProcessMemory(GetCurrentProcess(),packet,&value,sizeof(value),&bytes) || bytes!=sizeof(value)) return ERROR_INVALID_PARAMETER;
    int64_t target=(int64_t)recommended+value.scale_rel;
    if (target<0 || target>=UURB_DISPLAY_SCALE_COUNT || !(snapshot.scale_mask & (1u << (unsigned)target)))
        return ERROR_BAD_CONFIGURATION;
    struct uurb_display_request request={.version=1,.operation=UURB_DISPLAY_APPLY,
        .serial=snapshot.serial,.width=snapshot.current.width,.height=snapshot.current.height,
        .refresh_millihz=snapshot.current.refresh_millihz,.scale_milli=uurb_display_scale_milli((unsigned)target)};
    struct uurb_display_reply reply;
    DWORD status=native_display_change_api(native_display_endpoint,&request,&reply);
    if (status==UURB_DISPLAY_BAD_MODE || status==UURB_DISPLAY_INVALID) return ERROR_BAD_CONFIGURATION;
    if (!status && reply.changed) {
        AcquireSRWLockExclusive(&native_display_cache_lock);
        native_display_cache_time=0;
        ReleaseSRWLockExclusive(&native_display_cache_lock);
    }
    return status ? ERROR_GEN_FAILURE : ERROR_SUCCESS;
}
static LONG WINAPI native_geometry_device_info(DISPLAYCONFIG_DEVICE_INFO_HEADER *packet)
{ return native_geometry_device_info_common(packet, FALSE); }
static LONG WINAPI native_geometry_set_device_info(DISPLAYCONFIG_DEVICE_INFO_HEADER *packet)
{ return native_geometry_device_info_common(packet, TRUE); }

static BOOL native_display_current_mode(struct uurb_display_mode *mode)
{
    AcquireSRWLockExclusive(&native_display_cache_lock);
    BOOL ok=native_display_refresh(FALSE);
    if (ok) *mode=native_display_cache.current;
    ReleaseSRWLockExclusive(&native_display_cache_lock);
    return ok && mode->width>=2 && mode->height>=2 && mode->width<=8192 && mode->height<=8192 &&
        !(mode->width&1) && !(mode->height&1) && mode->refresh_millihz>=1000 && mode->refresh_millihz<=120000;
}
static RECT native_display_pixel_rect(const struct uurb_display_mode *mode)
{ return (RECT){0,0,(LONG)mode->width,(LONG)mode->height}; }

static BOOL native_monitor_geometry(LPMONITORINFO output)
{
    if (!(output->dwFlags&MONITORINFOF_PRIMARY)) return TRUE;
    struct uurb_display_mode mode;
    if (!native_display_current_mode(&mode)) { SetLastError(ERROR_GEN_FAILURE);return FALSE; }
    /* The hidden Wine desktop has no native shell work-area reservation.
     * Do not retain its stale Xvfb rectangle as a work area after resizing. */
    output->rcMonitor=output->rcWork=native_display_pixel_rect(&mode);
    return TRUE;
}
static BOOL WINAPI native_geometry_monitor_w(HMONITOR monitor,LPMONITORINFO output)
{
    BOOL result=geometry_monitor_w(monitor,output);DWORD saved=GetLastError();
    if (result && native_display_adapter_enabled && !native_monitor_geometry(output)) return FALSE;
    SetLastError(saved);return result;
}
static BOOL WINAPI native_geometry_monitor_a(HMONITOR monitor,LPMONITORINFO output)
{
    BOOL result=geometry_monitor_a(monitor,output);DWORD saved=GetLastError();
    if (result && native_display_adapter_enabled && !native_monitor_geometry(output)) return FALSE;
    SetLastError(saved);return result;
}
static int WINAPI native_geometry_metrics(int index)
{
    if (!native_display_adapter_enabled || (index!=SM_CXSCREEN && index!=SM_CYSCREEN &&
        index!=SM_CXVIRTUALSCREEN && index!=SM_CYVIRTUALSCREEN && index!=SM_XVIRTUALSCREEN && index!=SM_YVIRTUALSCREEN))
        return geometry_metrics(index);
    DWORD saved=GetLastError();struct uurb_display_mode mode;
    if (!native_display_current_mode(&mode)) { SetLastError(ERROR_GEN_FAILURE);return 0; }
    SetLastError(saved);
    if (index==SM_CXSCREEN || index==SM_CXVIRTUALSCREEN) return (int)mode.width;
    if (index==SM_CYSCREEN || index==SM_CYVIRTUALSCREEN) return (int)mode.height;
    return 0;
}
struct native_monitor_enum_context {
    MONITORENUMPROC callback;LPARAM data;RECT rectangle,clip;BOOL clipped;
};
static BOOL CALLBACK native_geometry_enum_callback(HMONITOR monitor,HDC dc,LPRECT rectangle,LPARAM data)
{
    struct native_monitor_enum_context *context=(void *)data;
    MONITORINFO info={.cbSize=sizeof(info)};
    if (!geometry_monitor_w(monitor,&info)) return FALSE;
    RECT selected=info.dwFlags&MONITORINFOF_PRIMARY ? context->rectangle : *rectangle;
    if (context->clipped && !IntersectRect(&selected,&selected,&context->clip)) return TRUE;
    return context->callback(monitor,dc,&selected,context->data);
}
static BOOL WINAPI native_geometry_monitors(HDC dc,LPCRECT clip,MONITORENUMPROC callback,LPARAM data)
{
    /* HDC-backed enumeration belongs to an actual Wine drawing surface, not
     * our native capture. Preserve that API's coordinate/clip semantics. */
    if (!native_display_adapter_enabled || dc || !callback) return geometry_monitors(dc,clip,callback,data);
    struct uurb_display_mode mode;
    if (!native_display_current_mode(&mode)) { SetLastError(ERROR_GEN_FAILURE);return FALSE; }
    struct native_monitor_enum_context context={.callback=callback,.data=data,
        .rectangle=native_display_pixel_rect(&mode),.clipped=clip!=NULL};
    if (clip) {
        SIZE_T bytes;
        if (!ReadProcessMemory(GetCurrentProcess(),clip,&context.clip,sizeof(context.clip),&bytes) || bytes!=sizeof(context.clip)) {
            SetLastError(ERROR_INVALID_PARAMETER);return FALSE;
        }
    }
    /* Do not pass the new-coordinate clip to the old Xvfb rectangle filter. */
    return geometry_monitors(NULL,NULL,native_geometry_enum_callback,(LPARAM)&context);
}
static BOOL native_display_same_luid(LUID a,LUID b)
{ return a.LowPart==b.LowPart && a.HighPart==b.HighPart; }

static LONG native_display_map_path(UINT32 flags,DISPLAYCONFIG_PATH_INFO *path,UINT32 count,
                                    DISPLAYCONFIG_MODE_INFO *modes,const struct uurb_display_mode *geometry)
{
    if ((flags&~QDC_VIRTUAL_MODE_AWARE)!=QDC_ONLY_ACTIVE_PATHS &&
        (flags&~QDC_VIRTUAL_MODE_AWARE)!=QDC_ALL_PATHS) return ERROR_NOT_SUPPORTED;
    if (!path || !modes || count<2 || count>3 || !(path->flags&DISPLAYCONFIG_PATH_ACTIVE) || !path->targetInfo.targetAvailable)
        return ERROR_NOT_SUPPORTED;
    BOOL virt=!!(path->flags&DISPLAYCONFIG_PATH_SUPPORT_VIRTUAL_MODE);
    if (virt && !(flags&QDC_VIRTUAL_MODE_AWARE)) return ERROR_INVALID_DATA;
    UINT32 source=virt ? path->sourceInfo.sourceModeInfoIdx : path->sourceInfo.modeInfoIdx;
    UINT32 target=virt ? path->targetInfo.targetModeInfoIdx : path->targetInfo.modeInfoIdx;
    UINT32 desktop=virt ? path->targetInfo.desktopModeInfoIdx : DISPLAYCONFIG_PATH_DESKTOP_IMAGE_IDX_INVALID;
    if (source>=count || target>=count || source==target ||
        modes[source].infoType!=DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE || modes[target].infoType!=DISPLAYCONFIG_MODE_INFO_TYPE_TARGET ||
        modes[source].id!=path->sourceInfo.id || modes[target].id!=path->targetInfo.id ||
        !native_display_same_luid(modes[source].adapterId,path->sourceInfo.adapterId) ||
        !native_display_same_luid(modes[target].adapterId,path->targetInfo.adapterId)) return ERROR_INVALID_DATA;
    if (desktop!=DISPLAYCONFIG_PATH_DESKTOP_IMAGE_IDX_INVALID &&
        (desktop>=count || desktop==source || desktop==target ||
         modes[desktop].infoType!=DISPLAYCONFIG_MODE_INFO_TYPE_DESKTOP_IMAGE ||
         !native_display_same_luid(modes[desktop].adapterId,path->sourceInfo.adapterId) ||
         modes[desktop].id!=path->sourceInfo.id)) return ERROR_INVALID_DATA;
    if (count!=(desktop==DISPLAYCONFIG_PATH_DESKTOP_IMAGE_IDX_INVALID ? 2u : 3u)) return ERROR_NOT_SUPPORTED;
    /* Preserve the actual Windows IDs, mode indices and flags. Only replace
     * the geometry/timing of this Wine-backed native display view. Synthetic
     * zero-blanking timing describes the virtual view, not physical DP EDID. */
    DISPLAYCONFIG_MODE_INFO mapped[3];memcpy(mapped,modes,count*sizeof(*modes));
    DISPLAYCONFIG_PATH_INFO next=*path;
    mapped[source].sourceMode=(DISPLAYCONFIG_SOURCE_MODE){.width=geometry->width,.height=geometry->height,
        .pixelFormat=DISPLAYCONFIG_PIXELFORMAT_32BPP,.position={0,0}};
    DISPLAYCONFIG_VIDEO_SIGNAL_INFO signal={0};
    signal.activeSize.cx=signal.totalSize.cx=geometry->width;
    signal.activeSize.cy=signal.totalSize.cy=geometry->height;
    signal.vSyncFreq=(DISPLAYCONFIG_RATIONAL){geometry->refresh_millihz,1000};
    signal.hSyncFreq=(DISPLAYCONFIG_RATIONAL){geometry->height*geometry->refresh_millihz,1000};
    signal.pixelRate=(UINT64)geometry->width*geometry->height*geometry->refresh_millihz/1000;
    signal.videoStandard=255;signal.scanLineOrdering=DISPLAYCONFIG_SCANLINE_ORDERING_PROGRESSIVE;
    mapped[target].targetMode.targetVideoSignalInfo=signal;
    next.targetInfo.refreshRate=signal.vSyncFreq;
    next.targetInfo.scanLineOrdering=signal.scanLineOrdering;
    next.targetInfo.rotation=DISPLAYCONFIG_ROTATION_IDENTITY;
    next.targetInfo.scaling=DISPLAYCONFIG_SCALING_IDENTITY;
    if (desktop!=DISPLAYCONFIG_PATH_DESKTOP_IMAGE_IDX_INVALID) {
        DISPLAYCONFIG_DESKTOP_IMAGE_INFO image={.PathSourceSize={(LONG)geometry->width,(LONG)geometry->height},
            .DesktopImageRegion={0,0,(LONG)geometry->width,(LONG)geometry->height},
            .DesktopImageClip={0,0,(LONG)geometry->width,(LONG)geometry->height}};
        _Static_assert(sizeof(image)<=sizeof(mapped[desktop].targetMode),"desktop image fits Windows mode union");
        memcpy(&mapped[desktop].targetMode,&image,sizeof(image)); /* Older MinGW omits its union member. */
    }
    *path=next;memcpy(modes,mapped,count*sizeof(*modes));return ERROR_SUCCESS;
}
static LONG native_display_query_geometry(UINT32 flags,UINT32 paths,DISPLAYCONFIG_PATH_INFO *path,
                                          UINT32 count,DISPLAYCONFIG_MODE_INFO *modes)
{
    if (paths!=1 || !path || !modes || !geometry_device_info) return ERROR_NOT_SUPPORTED;
    DISPLAYCONFIG_SOURCE_DEVICE_NAME name={.header={.type=DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
        .size=sizeof(name),.adapterId=path->sourceInfo.adapterId,.id=path->sourceInfo.id}};
    if (geometry_device_info(&name.header) || !display_primary(name.viewGdiDeviceName)) return ERROR_NOT_SUPPORTED;
    struct uurb_display_mode geometry;
    if (!native_display_current_mode(&geometry)) return ERROR_GEN_FAILURE;
    return native_display_map_path(flags,path,count,modes,&geometry);
}
static void *native_geometry_hook_for(uintptr_t original)
{
    if (!original) return NULL;
#define GEOMETRY_MAP(a,b) if(original==(uintptr_t)a)return (void *)&b
    GEOMETRY_MAP(geometry_monitor_w,native_geometry_monitor_w);
    GEOMETRY_MAP(geometry_monitor_a,native_geometry_monitor_a);
    GEOMETRY_MAP(geometry_monitors,native_geometry_monitors);
    GEOMETRY_MAP(geometry_metrics,native_geometry_metrics);
    GEOMETRY_MAP(geometry_device_info,native_geometry_device_info);
    GEOMETRY_MAP(geometry_device_set_info,native_geometry_set_device_info);
#undef GEOMETRY_MAP
    return NULL;
}
static void native_geometry_initialize(void)
{
    HMODULE module=GetModuleHandleW(L"user32.dll");
#define GEOMETRY_GET(a,b) a=(void *)GetProcAddress(module,b)
    GEOMETRY_GET(geometry_monitor_w,"GetMonitorInfoW");
    GEOMETRY_GET(geometry_monitor_a,"GetMonitorInfoA");
    GEOMETRY_GET(geometry_monitors,"EnumDisplayMonitors");
    GEOMETRY_GET(geometry_metrics,"GetSystemMetrics");
    GEOMETRY_GET(geometry_device_info,"DisplayConfigGetDeviceInfo");
    GEOMETRY_GET(geometry_device_set_info,"DisplayConfigSetDeviceInfo");
#undef GEOMETRY_GET
}
#endif
