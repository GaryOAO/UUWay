#ifndef UURB_NATIVE_DISPLAY_SET_WIN_H
#define UURB_NATIVE_DISPLAY_SET_WIN_H
#define UURB_DISPLAY_SET_IMPLEMENTATION_VERSION 1u
/* Explicit supplied single-path SetDisplayConfig, using the native guardian.
 * No topology/database emulation or Wine desktop mode changes. Included after
 * the geometry adapter. Identity comes from the real active Windows path. */
static LONG native_display_plan_set(UINT32 paths, const DISPLAYCONFIG_PATH_INFO *path,
    UINT32 count, const DISPLAYCONFIG_MODE_INFO *modes, UINT32 flags,
    const DISPLAYCONFIG_PATH_INFO *current, struct uurb_display_request *request)
{
    UINT32 allowed=SDC_USE_SUPPLIED_DISPLAY_CONFIG|SDC_APPLY|SDC_VALIDATE|SDC_ALLOW_CHANGES|SDC_VIRTUAL_MODE_AWARE;
    if (!(flags&SDC_USE_SUPPLIED_DISPLAY_CONFIG) ||
        !!(flags&SDC_APPLY)==!!(flags&SDC_VALIDATE)) return ERROR_INVALID_PARAMETER;
    if (flags&~allowed) return ERROR_NOT_SUPPORTED;
    if (paths!=1 || !path || !current || !modes || !count || count>3) return ERROR_NOT_SUPPORTED;
    if (!(path->flags&DISPLAYCONFIG_PATH_ACTIVE) ||
        path->flags&~(DISPLAYCONFIG_PATH_ACTIVE|DISPLAYCONFIG_PATH_SUPPORT_VIRTUAL_MODE) ||
        path->sourceInfo.id!=current->sourceInfo.id || path->targetInfo.id!=current->targetInfo.id ||
        !native_display_same_luid(path->sourceInfo.adapterId,current->sourceInfo.adapterId) ||
        !native_display_same_luid(path->targetInfo.adapterId,current->targetInfo.adapterId) ||
        path->targetInfo.outputTechnology!=current->targetInfo.outputTechnology ||
        path->targetInfo.rotation!=DISPLAYCONFIG_ROTATION_IDENTITY ||
        path->targetInfo.scaling!=DISPLAYCONFIG_SCALING_IDENTITY) return ERROR_NOT_SUPPORTED;
    BOOL virt=!!(path->flags&DISPLAYCONFIG_PATH_SUPPORT_VIRTUAL_MODE);
    if (virt && !(flags&SDC_VIRTUAL_MODE_AWARE)) return ERROR_INVALID_PARAMETER;
    /* This is a reported capability, not a caller-requested topology change. */
    if (virt!=!!(current->flags&DISPLAYCONFIG_PATH_SUPPORT_VIRTUAL_MODE)) return ERROR_NOT_SUPPORTED;
    UINT32 source=virt ? path->sourceInfo.sourceModeInfoIdx : path->sourceInfo.modeInfoIdx;
    UINT32 target=virt ? path->targetInfo.targetModeInfoIdx : path->targetInfo.modeInfoIdx;
    UINT32 desktop=virt ? path->targetInfo.desktopModeInfoIdx : DISPLAYCONFIG_PATH_DESKTOP_IMAGE_IDX_INVALID;
    BOOL target_present=target!=(virt ? DISPLAYCONFIG_PATH_TARGET_MODE_IDX_INVALID : DISPLAYCONFIG_PATH_MODE_IDX_INVALID);
    BOOL desktop_present=desktop!=DISPLAYCONFIG_PATH_DESKTOP_IMAGE_IDX_INVALID;
    if (source>=count || modes[source].infoType!=DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE ||
        modes[source].id!=path->sourceInfo.id ||
        !native_display_same_luid(modes[source].adapterId,path->sourceInfo.adapterId)) return ERROR_INVALID_PARAMETER;
    if (target_present && (target>=count || target==source || modes[target].infoType!=DISPLAYCONFIG_MODE_INFO_TYPE_TARGET ||
        modes[target].id!=path->targetInfo.id ||
        !native_display_same_luid(modes[target].adapterId,path->targetInfo.adapterId))) return ERROR_INVALID_PARAMETER;
    if (desktop_present && (desktop>=count || desktop==source || (target_present && desktop==target) ||
        modes[desktop].infoType!=DISPLAYCONFIG_MODE_INFO_TYPE_DESKTOP_IMAGE || modes[desktop].id!=path->sourceInfo.id ||
        !native_display_same_luid(modes[desktop].adapterId,path->sourceInfo.adapterId))) return ERROR_INVALID_PARAMETER;
    if (count!=1u+target_present+desktop_present) return ERROR_NOT_SUPPORTED;
    const DISPLAYCONFIG_SOURCE_MODE *mode=&modes[source].sourceMode;
    if (mode->width<2 || mode->height<2 || mode->width>4096 || mode->height>4096 ||
        (mode->width&1) || (mode->height&1) || mode->position.x || mode->position.y ||
        mode->pixelFormat!=DISPLAYCONFIG_PIXELFORMAT_32BPP) return ERROR_BAD_CONFIGURATION;
    const DISPLAYCONFIG_VIDEO_SIGNAL_INFO *signal=target_present ? &modes[target].targetMode.targetVideoSignalInfo : NULL;
    /* Target-mode fields supersede path hints per the CCD contract. */
    DISPLAYCONFIG_RATIONAL rate=signal ? signal->vSyncFreq : path->targetInfo.refreshRate;
    DISPLAYCONFIG_SCANLINE_ORDERING scan=signal ? signal->scanLineOrdering : path->targetInfo.scanLineOrdering;
    if ((!rate.Numerator)!=(!rate.Denominator)) return ERROR_INVALID_PARAMETER;
    if (!rate.Numerator && (signal || scan!=DISPLAYCONFIG_SCANLINE_ORDERING_UNSPECIFIED)) return ERROR_INVALID_PARAMETER;
    UINT64 millihz=rate.Denominator ? ((UINT64)rate.Numerator*1000+rate.Denominator/2)/rate.Denominator : 0;
    if (rate.Denominator && (millihz<1000 || millihz>120000)) return ERROR_BAD_CONFIGURATION;
    if (scan!=DISPLAYCONFIG_SCANLINE_ORDERING_PROGRESSIVE &&
        scan!=DISPLAYCONFIG_SCANLINE_ORDERING_UNSPECIFIED) return ERROR_NOT_SUPPORTED;
    if (target_present && !(flags&SDC_ALLOW_CHANGES)) {
        /* Exact supplied timings must be the canonical native view emitted by
         * our QueryDisplayConfig. Arbitrary physical timing programming is not
         * implemented. ALLOW_CHANGES permits the guardian to choose a real mode. */
        if (!millihz || signal->activeSize.cx!=mode->width || signal->activeSize.cy!=mode->height ||
            signal->totalSize.cx!=mode->width || signal->totalSize.cy!=mode->height ||
            !signal->vSyncFreq.Denominator || !signal->hSyncFreq.Denominator ||
            (UINT64)signal->vSyncFreq.Numerator*rate.Denominator!=(UINT64)rate.Numerator*signal->vSyncFreq.Denominator ||
            (UINT64)signal->hSyncFreq.Numerator*1000!=(UINT64)mode->height*millihz*signal->hSyncFreq.Denominator ||
            signal->pixelRate!=(UINT64)mode->width*mode->height*millihz/1000 ||
            signal->videoStandard!=255 || signal->scanLineOrdering!=DISPLAYCONFIG_SCANLINE_ORDERING_PROGRESSIVE)
            return ERROR_NOT_SUPPORTED;
    }
    if (desktop_present) {
        DISPLAYCONFIG_DESKTOP_IMAGE_INFO image;
        memcpy(&image,&modes[desktop].targetMode,sizeof(image));
        RECT expected={0,0,(LONG)mode->width,(LONG)mode->height};
        if (image.PathSourceSize.x!=(LONG)mode->width || image.PathSourceSize.y!=(LONG)mode->height ||
            memcmp(&image.DesktopImageRegion,&expected,sizeof(expected)) ||
            memcmp(&image.DesktopImageClip,&expected,sizeof(expected))) return ERROR_NOT_SUPPORTED;
    }
    *request=(struct uurb_display_request){.version=1,
        .operation=flags&SDC_VALIDATE ? UURB_DISPLAY_VERIFY : UURB_DISPLAY_APPLY,
        .width=mode->width,.height=mode->height,.refresh_millihz=(UINT32)millihz};
    return ERROR_SUCCESS;
}
static BOOL native_display_set_rate_safe(const struct uurb_display_snapshot *snapshot,
    const struct uurb_display_request *request, UINT32 flags)
{
    if (flags&SDC_ALLOW_CHANGES || !request->refresh_millihz) return TRUE;
    /* Legacy CDS accepts integer Hz with a 0.75-Hz tolerance and chooses the
     * fastest match. CCD without ALLOW_CHANGES cannot silently take that path.
     * Keep the existing guardian ABI: require an exact mHz candidate and reject
     * all other potentially selected rates. Snapshot has no per-mode scales,
     * so do not guess that a competing rate is ineligible at the current DPI.
     * The same serial accompanies the apply, fencing a concurrent mode change. */
    BOOL exact=FALSE;
    for (UINT32 i=0;i<snapshot->count && i<UURB_DISPLAY_MAX_MODES;++i) {
        const struct uurb_display_mode *mode=&snapshot->modes[i];
        if (mode->width!=request->width || mode->height!=request->height) continue;
        UINT32 difference=mode->refresh_millihz>request->refresh_millihz ?
            mode->refresh_millihz-request->refresh_millihz : request->refresh_millihz-mode->refresh_millihz;
        if (!difference) exact=TRUE;
        /* Include the rounding boundary; 1 Hz means default in the legacy
         * guardian, so every different rate could be selected in that case. */
        else if (request->refresh_millihz==1000 || difference<=750) return FALSE;
    }
    return exact;
}
static LONG native_display_set_config(UINT32 paths, DISPLAYCONFIG_PATH_INFO *path,
    UINT32 count, DISPLAYCONFIG_MODE_INFO *modes, UINT32 flags)
{
    if (paths!=1 || count<1 || count>3 || !path || !modes || !display_query || !geometry_device_info)
        return ERROR_NOT_SUPPORTED;
    DISPLAYCONFIG_PATH_INFO copied,current;
    DISPLAYCONFIG_MODE_INFO copied_modes[3],current_modes[3];SIZE_T bytes;
    if (!ReadProcessMemory(GetCurrentProcess(),path,&copied,sizeof(copied),&bytes) || bytes!=sizeof(copied) ||
        !ReadProcessMemory(GetCurrentProcess(),modes,copied_modes,count*sizeof(*modes),&bytes) || bytes!=count*sizeof(*modes))
        return ERROR_INVALID_PARAMETER;
    UINT32 current_paths=1,current_count=3;
    UINT32 query_flags=QDC_ONLY_ACTIVE_PATHS|((flags&SDC_VIRTUAL_MODE_AWARE) ? QDC_VIRTUAL_MODE_AWARE : 0);
    LONG result=display_query(query_flags,&current_paths,&current,&current_count,current_modes,NULL);
    if (result || current_paths!=1 || !current.targetInfo.targetAvailable ||
        !(current.flags&DISPLAYCONFIG_PATH_ACTIVE)) return ERROR_NOT_SUPPORTED;
    DISPLAYCONFIG_SOURCE_DEVICE_NAME name={.header={.type=DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
        .size=sizeof(name),.adapterId=current.sourceInfo.adapterId,.id=current.sourceInfo.id}};
    if (geometry_device_info(&name.header) || !display_primary(name.viewGdiDeviceName)) return ERROR_NOT_SUPPORTED;
    struct uurb_display_request request;
    result=native_display_plan_set(paths,&copied,count,copied_modes,flags,&current,&request);
    if (result) return result;
    BOOL notify=FALSE;
    AcquireSRWLockExclusive(&native_display_cache_lock);
    if (!native_display_refresh(TRUE)) { result=ERROR_GEN_FAILURE;goto done; }
    if (!native_display_set_rate_safe(&native_display_cache,&request,flags)) {
        result=ERROR_BAD_CONFIGURATION;goto done;
    }
    request.serial=native_display_cache.serial;
    struct uurb_display_reply reply;
    DWORD status=native_display_change_api(native_display_endpoint,&request,&reply);
    result=status==UURB_DISPLAY_BAD_MODE ? ERROR_BAD_CONFIGURATION :
        status==UURB_DISPLAY_INVALID ? ERROR_INVALID_PARAMETER : status ? ERROR_GEN_FAILURE : ERROR_SUCCESS;
    if (!status && reply.changed && request.operation==UURB_DISPLAY_APPLY) {
        native_display_cache_time=0;notify=TRUE;
    }
done:
    ReleaseSRWLockExclusive(&native_display_cache_lock);
    if (notify) SendNotifyMessageW(HWND_BROADCAST,WM_DISPLAYCHANGE,32,MAKELPARAM(request.width,request.height));
    return result;
}
#endif
