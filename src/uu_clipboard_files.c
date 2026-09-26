/* Save the virtual files UU puts on the Windows clipboard for a phone copy.
 *
 * UU offers a phone file as FileGroupDescriptorW plus one FileContents item
 * per file, fetched from the phone when a reader asks for that index, as
 * Explorer does on paste. Wine's X11 clipboard can only render FileContents
 * without an index, so the clipboard bridge runs this helper in UU's prefix:
 * it reads each file through OLE into an empty directory the bridge created,
 * then exits; the bridge offers the saved files to the desktop.
 *
 * usage: uu-clipboard-files.exe DIRECTORY MAX_BYTES
 * Exit 0: every item saved. Names that would leave DIRECTORY or that Windows
 * cannot create are refused, and saving stops once MAX_BYTES are written. */
#define COBJMACROS
#include <windows.h>
#include <ole2.h>
#include <shlobj.h>
#include <stdlib.h>
#include <wchar.h>

enum { OK, USAGE, NO_OLE, NO_CLIPBOARD, NO_FILES, TOO_LARGE, BAD_NAME, WRITE_FAILED, READ_FAILED };

/* Relative, and every component a plain Windows name: not empty, no
 * characters Windows forbids, and no trailing dot or space, which Windows
 * strips (so "..." would become ".." and "." would name the parent). */
static BOOL safe_name(const WCHAR *name)
{
    const WCHAR *component = name;

    if (!name[0] || name[0] == L'\\' || name[0] == L'/')
        return FALSE;
    for (;;) {
        size_t length = wcscspn(component, L"\\/");

        if (length == 0 || component[length - 1] == L'.' || component[length - 1] == L' ')
            return FALSE;
        for (size_t index = 0; index < length; index++)
            if (component[index] < 0x20 || wcschr(L"<>:\"|?*", component[index]))
                return FALSE;
        if (!component[length])
            return TRUE;
        component += length + 1;
    }
}

/* Create every directory above the file at `path`, which starts with `root`. */
static void make_parents(WCHAR *path, size_t root_length)
{
    for (WCHAR *cursor = path + root_length + 1; *cursor; cursor++) {
        if (*cursor == L'\\' || *cursor == L'/') {
            WCHAR saved = *cursor;

            *cursor = L'\0';
            CreateDirectoryW(path, NULL);
            *cursor = saved;
        }
    }
}

static BOOL write_all(HANDLE file, const void *data, DWORD size)
{
    DWORD written;

    return WriteFile(file, data, size, &written, NULL) && written == size;
}

/* Write `size` more bytes if the budget allows. */
static int write_within(HANDLE file, const void *data, SIZE_T size, ULONGLONG *budget)
{
    if (size > *budget)
        return TOO_LARGE;
    *budget -= size;
    return size <= MAXDWORD && write_all(file, data, (DWORD)size) ? OK : WRITE_FAILED;
}

static int save_contents(IDataObject *data, CLIPFORMAT format, LONG index,
                         const FILEDESCRIPTORW *descriptor, const WCHAR *path, ULONGLONG *budget)
{
    FORMATETC request = {format, NULL, DVASPECT_CONTENT, index, TYMED_ISTREAM | TYMED_HGLOBAL};
    STGMEDIUM medium;
    HANDLE file;
    int result = OK;

    if (FAILED(IDataObject_GetData(data, &request, &medium)))
        return READ_FAILED;
    file = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        ReleaseStgMedium(&medium);
        return WRITE_FAILED;
    }
    if (medium.tymed == TYMED_ISTREAM) {
        static BYTE buffer[1 << 16];
        ULONG received;

        do {
            if (FAILED(IStream_Read(medium.pstm, buffer, sizeof(buffer), &received))) {
                result = READ_FAILED;
                break;
            }
            if (received)
                result = write_within(file, buffer, received, budget);
        } while (received == sizeof(buffer) && result == OK);
    } else if (medium.tymed == TYMED_HGLOBAL) {
        SIZE_T size = GlobalSize(medium.hGlobal);
        const BYTE *bytes = GlobalLock(medium.hGlobal);

        /* A global block may be rounded up past the file's end. */
        if (descriptor->dwFlags & FD_FILESIZE && descriptor->nFileSizeHigh == 0 &&
            descriptor->nFileSizeLow < size)
            size = descriptor->nFileSizeLow;
        result = bytes != NULL ? write_within(file, bytes, size, budget) : READ_FAILED;
        GlobalUnlock(medium.hGlobal);
    } else {
        result = READ_FAILED;
    }
    if (result == OK && descriptor->dwFlags & FD_WRITESTIME)
        SetFileTime(file, NULL, NULL, &descriptor->ftLastWriteTime);
    CloseHandle(file);
    ReleaseStgMedium(&medium);
    return result;
}

static int save_files(IDataObject *data, const WCHAR *directory, ULONGLONG limit)
{
    FORMATETC request = {(CLIPFORMAT)RegisterClipboardFormatW(L"FileGroupDescriptorW"), NULL,
                         DVASPECT_CONTENT, -1, TYMED_HGLOBAL};
    CLIPFORMAT contents = (CLIPFORMAT)RegisterClipboardFormatW(L"FileContents");
    size_t root_length = wcslen(directory);
    ULONGLONG total = 0;
    FILEGROUPDESCRIPTORW *group;
    STGMEDIUM medium;
    SIZE_T size;
    int result = OK;

    if (FAILED(IDataObject_GetData(data, &request, &medium)))
        return NO_FILES;
    size = GlobalSize(medium.hGlobal);
    group = GlobalLock(medium.hGlobal);
    if (group == NULL || size < sizeof(UINT) || group->cItems == 0 ||
        (size - sizeof(UINT)) / sizeof(FILEDESCRIPTORW) < group->cItems) {
        result = NO_FILES;
        goto done;
    }
    for (UINT index = 0; index < group->cItems; index++) {
        const FILEDESCRIPTORW *descriptor = &group->fgd[index];

        if (wmemchr(descriptor->cFileName, L'\0', MAX_PATH) == NULL || !safe_name(descriptor->cFileName)) {
            result = BAD_NAME;
            goto done;
        }
        /* Refuse early when the declared sizes already exceed the limit;
         * the written bytes are counted against it as well. */
        if (descriptor->dwFlags & FD_FILESIZE) {
            ULONGLONG declared = ((ULONGLONG)descriptor->nFileSizeHigh << 32) | descriptor->nFileSizeLow;

            if (declared > limit - total) {
                result = TOO_LARGE;
                goto done;
            }
            total += declared;
        }
    }
    for (UINT index = 0; index < group->cItems && result == OK; index++) {
        const FILEDESCRIPTORW *descriptor = &group->fgd[index];
        size_t length = root_length + 1 + wcslen(descriptor->cFileName) + 1;
        WCHAR *path = malloc(length * sizeof(WCHAR));

        swprintf(path, length, L"%ls\\%ls", directory, descriptor->cFileName);
        make_parents(path, root_length);
        if (descriptor->dwFlags & FD_ATTRIBUTES &&
            descriptor->dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            CreateDirectoryW(path, NULL);
        else
            result = save_contents(data, contents, (LONG)index, descriptor, path, &limit);
        free(path);
    }
done:
    GlobalUnlock(medium.hGlobal);
    ReleaseStgMedium(&medium);
    return result;
}

int wmain(int argc, WCHAR **argv)
{
    IDataObject *data;
    int result;

    if (argc != 3)
        return USAGE;
    if (FAILED(OleInitialize(NULL)))
        return NO_OLE;
    if (FAILED(OleGetClipboard(&data))) {
        OleUninitialize();
        return NO_CLIPBOARD;
    }
    result = save_files(data, argv[1], _wcstoui64(argv[2], NULL, 10));
    IDataObject_Release(data);
    OleUninitialize();
    return result;
}
