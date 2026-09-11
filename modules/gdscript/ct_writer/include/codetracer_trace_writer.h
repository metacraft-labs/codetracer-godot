#ifndef CODETRACER_TRACE_WRITER_H
#define CODETRACER_TRACE_WRITER_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque handle to the trace writer */
typedef void* trace_writer_t;

/* --------------------------------------------------------------------------
 * FFI enums (must match Rust codetracer_trace_writer_ffi repr(C) values)
 * -------------------------------------------------------------------------- */

enum FfiTraceFormat {
    FFI_TRACE_FORMAT_JSON = 0,
    FFI_TRACE_FORMAT_BINARY_V0 = 1,
    FFI_TRACE_FORMAT_BINARY = 2
};

enum FfiTypeKind {
    FFI_TYPE_SEQ = 0,
    FFI_TYPE_SET = 1,
    FFI_TYPE_HASH_SET = 2,
    FFI_TYPE_ORDERED_SET = 3,
    FFI_TYPE_ARRAY = 4,
    FFI_TYPE_VARARGS = 5,
    FFI_TYPE_STRUCT = 6,
    FFI_TYPE_INT = 7,
    FFI_TYPE_FLOAT = 8,
    FFI_TYPE_STRING = 9,
    FFI_TYPE_CSTRING = 10,
    FFI_TYPE_CHAR = 11,
    FFI_TYPE_BOOL = 12,
    FFI_TYPE_LITERAL = 13,
    FFI_TYPE_REF = 14,
    FFI_TYPE_RECURSION = 15,
    FFI_TYPE_RAW = 16,
    FFI_TYPE_ENUM = 17,
    FFI_TYPE_ENUM16 = 18,
    FFI_TYPE_ENUM32 = 19,
    FFI_TYPE_C = 20,
    FFI_TYPE_TABLE_KIND = 21,
    FFI_TYPE_UNION = 22,
    FFI_TYPE_POINTER = 23,
    FFI_TYPE_ERROR = 24,
    FFI_TYPE_FUNCTION_KIND = 25,
    FFI_TYPE_TYPE_VALUE = 26,
    FFI_TYPE_TUPLE = 27,
    FFI_TYPE_VARIANT = 28,
    FFI_TYPE_HTML = 29,
    FFI_TYPE_NONE = 30,
    FFI_TYPE_NON_EXPANDED = 31,
    FFI_TYPE_ANY = 32,
    FFI_TYPE_SLICE = 33
};

enum FfiEventLogKind {
    FFI_EVENT_WRITE = 0,
    FFI_EVENT_WRITE_FILE = 1,
    FFI_EVENT_WRITE_OTHER = 2,
    FFI_EVENT_READ = 3,
    FFI_EVENT_READ_FILE = 4,
    FFI_EVENT_READ_OTHER = 5,
    FFI_EVENT_READ_DIR = 6,
    FFI_EVENT_OPEN_DIR = 7,
    FFI_EVENT_CLOSE_DIR = 8,
    FFI_EVENT_SOCKET = 9,
    FFI_EVENT_OPEN = 10,
    FFI_EVENT_ERROR = 11,
    FFI_EVENT_TRACE_LOG_EVENT = 12,
    FFI_EVENT_EVM_EVENT = 13
};

/* --------------------------------------------------------------------------
 * Initialization (call once before using any other function)
 * -------------------------------------------------------------------------- */

void codetracer_trace_writer_init(void);

/* --------------------------------------------------------------------------
 * Error handling
 * -------------------------------------------------------------------------- */

const char* trace_writer_last_error(void);

/*
 * Reset this thread's error buffer to "".
 *
 * trace_writer_last_error() is STICKY: nothing on a success path clears it,
 * so a non-empty buffer does NOT mean "the call I just made failed" — it may
 * be a message an earlier call left behind.  Clear it before a call whose
 * error you intend to attribute, and assert it is empty at that point.
 * Without this entry point that is not expressible from C, and a check that
 * only asserts "non-empty" passes on a stale message.
 *
 * trace_writer_register_path_version and trace_writer_current_path_id clear
 * it on entry themselves, so for those two a non-empty buffer afterwards is
 * always that call's message.
 */
void trace_writer_clear_last_error(void);

/* --------------------------------------------------------------------------
 * Lifecycle
 * -------------------------------------------------------------------------- */

trace_writer_t trace_writer_new(const char* program, int format);
void trace_writer_free(trace_writer_t handle);
int trace_writer_close(trace_writer_t handle);

/* --------------------------------------------------------------------------
 * File I/O — begin / finish (compatibility with Rust API)
 * -------------------------------------------------------------------------- */

int trace_writer_begin_metadata(trace_writer_t handle, const char* path);
int trace_writer_finish_metadata(trace_writer_t handle);
int trace_writer_begin_events(trace_writer_t handle, const char* path);
int trace_writer_finish_events(trace_writer_t handle);
int trace_writer_begin_paths(trace_writer_t handle, const char* path);
int trace_writer_finish_paths(trace_writer_t handle);

/* --------------------------------------------------------------------------
 * In-memory container — the filesystem-free alternative to begin_events
 *
 * trace_writer_begin_events derives a .ct path from the events path it is
 * given, and trace_writer_close opens that path and writes to it.  An embedder
 * with no filesystem -- a wasm module, or a host that wants the bytes rather
 * than a file -- calls trace_writer_begin_in_memory INSTEAD.  Everything
 * between begin and close is identical; only where the container ends up
 * differs.
 *
 * Call ONE of the two.  Both begins are idempotent no-ops on an already-open
 * writer, so the second one made is REFUSED (non-zero, with a message naming
 * the mode) rather than silently ignored.
 *
 * After trace_writer_close:
 *
 *     if (trace_writer_container_ready(w)) {
 *         const uint8_t* p = trace_writer_container_ptr(w);
 *         size_t         n = trace_writer_container_len(w);
 *         ...                         // p is valid until trace_writer_free
 *     }
 *
 * Read the READY flag rather than testing the length: an empty container is a
 * legitimate result (a recording with no events still carries a meta.dat), so
 * a zero length cannot stand in for "not finished yet".  The pointer is NULL
 * for a zero-length container and the bytes are owned by the handle -- they
 * are NOT to be freed by the caller, and they die with trace_writer_free.
 * -------------------------------------------------------------------------- */

int      trace_writer_begin_in_memory(trace_writer_t handle);
int      trace_writer_container_ready(trace_writer_t handle);
size_t   trace_writer_container_len(trace_writer_t handle);
uint8_t* trace_writer_container_ptr(trace_writer_t handle);

/* --------------------------------------------------------------------------
 * Tracing primitives
 * -------------------------------------------------------------------------- */

/*
 * Pin the recording's canonical UUIDv7 identity (M-REC-1, spec §3).
 *
 * MISSING FROM THIS HEADER UNTIL GDH-M6, and the omission had the same shape
 * as the one GDH-M3 fixed for the versioned-path calls: the entry point has
 * been exported by the FFI since M-REC-1, this header is what the Godot fork
 * vendors, so the fork could not pin an identity — and WITHOUT A PIN NO TWO
 * RECORDINGS OF THE SAME PROGRAM ARE EVER BYTE-IDENTICAL. Measured on
 * 2026-09-11 and re-measured at review: two runs of one engine over one
 * fixture differ ONLY inside the 36-character UUIDv7 stored in meta.dat, and
 * in nothing else in the container — no timestamp, no hash, no offset. The
 * NUMBER of differing bytes is not a constant (16 and 21 on two separate
 * pairs); it is just how many characters two random UUIDv7s happen to
 * disagree on, so do not write a check against it. The localisation is the
 * fact: pin the id and the containers are equal, byte for byte. Any gate that
 * asks whether a change altered a recording is unrunnable until a caller can
 * say "use this id".
 *
 * MUST be called BEFORE trace_writer_begin_events / begin_in_memory: the
 * constructors resolve the identity themselves, so a writer that is already
 * open is REFUSED rather than silently rewritten.
 *
 * An empty string is refused rather than treated as "mint one" — a caller
 * whose own id is missing must find that out, not have it papered over.
 *
 * Returns 0 on success, non-zero on refusal (see trace_writer_last_error).
 */
int trace_writer_set_recording_id(trace_writer_t handle,
                                  const char* recording_id);

void trace_writer_start(trace_writer_t handle, const char* path, int64_t line);
void trace_writer_set_workdir(trace_writer_t handle, const char* workdir);
/* IC-M2: stamp a fully-qualified-key origin namespace (the VM language, e.g.
 * "gdscript") on every interned string when this materialized writer shares a
 * container with the native recorder (MCR).  Call BEFORE trace_writer_begin_events.
 * Passing "" (or never calling it) keeps bare payloads, byte-identical to a
 * standalone trace. */
void trace_writer_set_interning_qualifier(trace_writer_t handle,
                                          const char* qualifier);
void trace_writer_register_step(trace_writer_t handle,
                                const char* path, int64_t line);

/* --------------------------------------------------------------------------
 * Per-file line counts (meta.dat bit 14) and versioned paths
 *
 * The first two entry points below have been exported by the FFI since the
 * line-count table landed and were MISSING FROM THIS HEADER until GDH-M3.
 * The consequence was concrete rather than cosmetic: this header is what the
 * Godot fork vendors, so the fork could not turn bit 14 on at all, and every
 * recording it produced laid every file out at the DefaultLinesPerFile
 * stride — under which a step past a file's real end is addressed inside the
 * NEXT file's range and read back as a (path, line) pair that was never
 * recorded, with nothing for the reader to refuse it against.
 * -------------------------------------------------------------------------- */

/*
 * Opt this writer into recording a per-file line count in every paths.dat
 * record (meta.dat bit 14).  Must be called BEFORE the first path is
 * registered, and is refused on a column-aware writer.
 *
 * After this call every path must be registered through
 * trace_writer_register_path_with_line_count: the implicit registration that
 * trace_writer_register_step performs for an unseen path has no count to
 * record and is refused by name.  A recorder that cannot count a file's lines
 * passes the ceiling it wants the file laid out with (conventionally 100000),
 * so the size the space uses is the size the container states.
 *
 * Returns 0 on success, non-zero on failure (see trace_writer_last_error).
 */
int trace_writer_enable_line_count_table(trace_writer_t handle);

/*
 * Register a source path together with the number of lines the file has,
 * which sizes the file's slot in the line-only global position space.
 * Only meaningful on a writer that called trace_writer_enable_line_count_table.
 * A line_count of 0 is refused rather than defaulted: a file sized 0 would
 * share its base with the next file.
 *
 * Returns 0 on success, non-zero on failure (see trace_writer_last_error).
 */
int trace_writer_register_path_with_line_count(trace_writer_t handle,
                                               const char* path,
                                               uint64_t line_count);

/*
 * The failure return of the two uint64_t-returning path entry points below.
 * A path id is an index into paths.dat, so UINT64_MAX is not a value either
 * call can legitimately produce.
 */
#define CT_TW_INVALID_PATH_ID ((uint64_t)0xFFFFFFFFFFFFFFFFULL)

/*
 * Register a NEW VERSION of an already-registered path and return THE
 * WRITER'S OWN id for it.
 *
 * This is what a hot-reload host calls after an external observer tells it a
 * source file changed.  It bypasses the interning lookup and always appends a
 * paths.dat record whose payload is byte-identical to the earlier version's:
 * the virtual path string is the same file, and only the INDEX discriminates
 * the version.  Nothing is appended to, prefixed to, or interposed into the
 * string, so a consumer that resolves a user-supplied path keeps resolving it
 * after a reload.
 *
 * The new version gets its own correctly sized slot in the global position
 * space, appended after every existing file, so addresses already emitted
 * against the old version keep decoding to it.  Requires
 * trace_writer_enable_line_count_table; without it a versioned record has
 * nowhere to put its size and the refusal names the missing table.
 *
 * A subsequent bare trace_writer_register_step(handle, path, line) on that
 * string resolves to the id returned here, so a recorder's hot path stays
 * version-unaware — only the reload path is version-aware.
 *
 * Returns CT_TW_INVALID_PATH_ID on failure, with trace_writer_last_error set
 * to a message naming the path.  The error buffer is cleared on entry.
 */
uint64_t trace_writer_register_path_version(trace_writer_t handle,
                                            const char* path,
                                            uint64_t line_count);

/*
 * The id a bare trace_writer_register_step(handle, path, ...) would attribute
 * a step to right now: the newest registered version of `path` when it has
 * been reloaded, and its ordinary interned id when it has not.
 *
 * THIS EXISTS SO A CALLER CAN DELETE ANY MIRROR OF THE WRITER'S INTERNING
 * COUNTER, NOT SO IT CAN KEEP ONE IN SYNC.  A host that re-derives path ids
 * by counting first sightings is correct only while the writer interns in
 * first-seen order from 0, and trace_writer_register_path_version makes that
 * false: from the first reload onward the mirror drifts, and every subsequent
 * trace_writer_register_source_view attaches to the WRONG FILE, silently.
 *
 * This is a pure query — it never registers the path it is asked about.  A
 * path this writer has never seen is a failure, not a fresh registration:
 * answering with a newly minted id would put a file in paths.dat that the
 * recording never executed, and under bit 14 would have to invent a size for
 * it.
 *
 * Returns CT_TW_INVALID_PATH_ID on failure, with trace_writer_last_error set.
 * The error buffer is cleared on entry.
 */
uint64_t trace_writer_current_path_id(trace_writer_t handle,
                                      const char* path);

/* --------------------------------------------------------------------------
 * Source-reload markers (GDH-M6 — design §6.3)
 *
 * The marker is what makes a reload DISCOVERABLE in the container rather
 * than inferable from the path indices.  A consumer scanning paths.dat for
 * a repeated string can guess that a reload happened; it cannot say WHEN in
 * the step stream, WHICH ids the transition ran between, or which wire
 * generation the new content carried.  Design §6.3.1 requires the marker for
 * that reason, so a host that mints a path version and emits no marker has
 * done half the job.
 *
 * Ordering matters and is not enforceable from here: emit the marker from
 * the SAME critical section that applies the reload, after
 * trace_writer_register_path_version has minted `new_path_id`.  A marker
 * emitted from a different lock hold can be separated from its apply by any
 * number of steps, and the container then states a boundary the execution
 * did not have.
 * -------------------------------------------------------------------------- */

/*
 * One file's transition across a reload boundary.  Three uint64s, no
 * padding, so a caller can build the array as a plain C struct literal.
 */
typedef struct {
    /* The paths.dat id this file's steps resolved to BEFORE the reload. */
    uint64_t old_path_id;
    /*
     * The id they resolve to after — the value
     * trace_writer_register_path_version returned.  MUST differ from
     * old_path_id: a reload that minted no new index cannot attribute its
     * post-reload steps to the version that ran them, and the call is
     * refused by name rather than recording a marker that says nothing.
     */
    uint64_t new_path_id;
    /*
     * The WIRE generation from the observer's notification.  Generation 1 is
     * the content the process started with, so a reload's generation is 2 or
     * more and a literal 1 is refused as a protocol error.  Deliberately off
     * by one from the container-side version ordinal.
     */
    uint64_t generation;
} ct_tw_source_reload_change;

/*
 * The failure return of trace_writer_register_source_reload.  reload_ordinal
 * is 1-based and monotonic within a trace, so 0 is not a value a successful
 * call can produce.
 */
#define CT_TW_INVALID_RELOAD_ORDINAL ((uint64_t)0)

/*
 * Emit a TagSourceReload marker at the current point in the execution stream
 * and return its 1-based reload_ordinal.
 *
 * `changed` must point to `changed_count` entries and `changed_count` must be
 * non-zero: a marker that records a reload without recording what it changed
 * cannot be told apart from one whose files were lost.
 *
 * `in_flight_frames` is the number of frames still executing the OLD
 * version's code when the marker was emitted (design §5.4).  Steps belonging
 * to those frames legitimately appear after the marker carrying the OLD path
 * id, so this is RECORDED rather than implied — a consumer must not read the
 * marker as a clean cut.  Pass 0 only when it really is one.
 *
 * The ordinal is the WRITER'S own count, not a caller-supplied number, so a
 * second reload cannot repeat the first's.
 *
 * Returns CT_TW_INVALID_RELOAD_ORDINAL (0) on failure, with
 * trace_writer_last_error set to a message naming the refusal.  The error
 * buffer is CLEARED on entry, so a non-empty buffer afterwards is always this
 * call's message and never a stale one.
 */
uint64_t trace_writer_register_source_reload(
    trace_writer_t handle,
    const ct_tw_source_reload_change* changed,
    size_t changed_count,
    uint64_t in_flight_frames);

/*
 * Markers emitted on this writer so far.  A host asserts against this rather
 * than against a counter of its own call sites: the two disagree exactly when
 * a call was refused, which is the case worth catching.
 */
uint64_t trace_writer_source_reload_count(trace_writer_t handle);

size_t trace_writer_ensure_function_id(trace_writer_t handle,
    const char* name, const char* path, int64_t line);

size_t trace_writer_ensure_type_id(trace_writer_t handle,
    int kind, const char* lang_type);

void trace_writer_register_call(trace_writer_t handle, size_t function_id);
void trace_writer_register_return(trace_writer_t handle);

void trace_writer_register_return_int(trace_writer_t handle,
                                      int64_t value,
                                      int type_kind,
                                      const char* type_name);

void trace_writer_register_return_raw(trace_writer_t handle,
                                      const char* value_repr,
                                      int type_kind,
                                      const char* type_name);

void trace_writer_register_variable_int(trace_writer_t handle,
                                        const char* name,
                                        int64_t value,
                                        int type_kind,
                                        const char* type_name);

void trace_writer_register_variable_raw(trace_writer_t handle,
                                        const char* name,
                                        const char* value_repr,
                                        int type_kind,
                                        const char* type_name);

void trace_writer_register_variable_cbor(trace_writer_t handle,
    const char* name,
    const uint8_t* cbor_data,
    size_t cbor_len);

/* Record `target_name = <rvalue>` on the step currently being buffered.
 *
 * `rvalue_cbor` / `rvalue_cbor_len` carry the serde-CBOR encoding of
 * `codetracer_trace_types::RValue` (adjacently tagged: `{"kind":…,"data":…}`).
 * `pass_by` is the `PassBy` discriminant in declaration order:
 * 0 = Value, 1 = Reference.
 *
 * The record reaches the trace as a tag-9 `Assignment` value-stream event
 * (trace-events.md §"Value Stream Events") inside the step's value record.
 *
 * Returns 0 on success, 1 on failure (see trace_writer_last_error).
 */
int trace_writer_register_assignment(trace_writer_t handle,
    const char* target_name,
    uint8_t pass_by,
    const uint8_t* rvalue_cbor,
    size_t rvalue_cbor_len);

void trace_writer_register_return_cbor(trace_writer_t handle,
    const uint8_t* cbor_data,
    size_t cbor_len);

void trace_writer_register_special_event(trace_writer_t handle,
    int kind, const char* metadata, const char* content);

/* --------------------------------------------------------------------------
 * Request / interval spans (RS-M1)
 *
 * A span is a bounded, labeled interval of execution — an HTTP request, a
 * process, a test — recorded into the container's spans.dat stream instead of
 * a session_manifest.jsonl / codetracer_spans.jsonl sidecar.  Spec:
 * codetracer-specs/Trace-Files/CTFS-Request-Span-Streams.md.
 *
 * Only the binary (multi-stream) backend supports spans.  Registering at
 * least one span sets meta.dat flag bit 13 (0x2000, FlagHasSpanStream) on the
 * finished container; a recording that registers none is byte-for-byte
 * unchanged.  NOTE that bit 13 is REJECTED by readers that predate it, so a
 * recorder should only emit spans once its consumers understand the bit.
 *
 * To publish an in-flight request, call once with SPAN_FLAG_OPEN set and
 * end_wall_ns / end_step zero, then call again on completion with the SAME
 * span_id; readers apply last-record-wins.  The stream is append-only.
 * -------------------------------------------------------------------------- */

/* `flags` bits */
#define SPAN_FLAG_OPEN     0x01u  /* open record; completion still to come */
#define SPAN_FLAG_EXTERNAL 0x02u  /* execution lives in another container */

/* `status` values */
#define SPAN_STATUS_UNKNOWN 0u
#define SPAN_STATUS_OK      1u
#define SPAN_STATUS_ERROR   2u

/* `structural` bits (Trace-Spans.md 2.4) */
#define SPAN_STRUCTURAL_CONTIGUOUS      0x01u /* uninterrupted, one thread   */
#define SPAN_STRUCTURAL_SHARES_TIMELINE 0x02u /* ordering comparable         */
#define SPAN_STRUCTURAL_CONCURRENT      0x04u /* siblings may overlap        */

/*
 * external_recording / external_path are read ONLY when SPAN_FLAG_EXTERNAL is
 * set (pass NULL otherwise).  metadata_keys / metadata_values are parallel
 * arrays of NUL-terminated UTF-8 strings of length metadata_count; their ORDER
 * IS PRESERVED end to end, so emit the well-known HTTP keys in display order.
 * Returns 0 on success, non-zero on failure (see trace_writer_last_error).
 */
int trace_writer_register_span(trace_writer_t handle,
    uint64_t span_id,
    uint64_t parent_span_id,
    uint8_t flags,
    uint8_t status,
    uint64_t start_wall_ns,
    uint64_t end_wall_ns,
    uint64_t process_ord,
    uint64_t thread_id,
    uint64_t start_step,
    uint64_t end_step,
    const char* external_recording,
    const char* external_path,
    const char* span_type,
    const char* label,
    uint8_t structural,
    const char** metadata_keys,
    const char** metadata_values,
    size_t metadata_count);

/*
 * Seal the current partial span chunk without closing the writer: the spans
 * registered so far are compressed into spans.dat and published in spans.idx,
 * so they are committed to the container instead of sitting in the writer's
 * buffer.  trace_writer_close flushes anyway, so batch recorders never need
 * this call.
 *
 * NOTE: this does NOT make the spans visible to a concurrent reader today.
 * The multi-stream writer builds the container in memory and the .ct file is
 * written only by trace_writer_close, so nothing appears on disk mid-session.
 * Live tailing would additionally require the writer to be created in
 * streaming mode; the span stream's write/sync ordering and its tailing
 * reader are already built for that.
 *
 * Returns 0 on success.
 */
int trace_writer_flush_spans(trace_writer_t handle);

/* ------------------------------------------------------------------------
 * Correlation markers
 *
 * Implemented ONCE here, as trace_writer_register_span is, so the ~20 CTFS
 * recorders bind to it rather than each constructing the on-disk payload. A
 * recorder whose field names drifted would write markers that are INVISIBLE
 * rather than broken, and nothing would report an error.
 *
 * Every string is (pointer, length), never NUL-terminated: a host string may
 * legally contain NUL (Ruby's can), and NUL-terminated marshalling both
 * truncates it and, on that side, raises from rb_string_value_cstr — a known
 * process-wedge regression.
 *
 * key_value / show_value must ALREADY be stringified UTF-8. This library
 * never calls back into the host to render a value: a conversion that can
 * raise must run before the binding takes the writer lock, because a host
 * exception can longjmp past the lock guard's destructor and wedge the
 * process permanently.
 *
 * A binding owns the no-op-when-not-recording behaviour. User code calls
 * these unconditionally, and "no active recording" is not an error there.
 *
 * All return 0 on success and non-zero on failure; see
 * trace_writer_last_error.
 * ------------------------------------------------------------------------ */

/*
 * Intern a boundary label and write its id to *out_id.
 *
 * THE PRIMARY OPERATION, mirroring path interning. Call it ONCE per boundary,
 * outside the hot path, then pass the integer to
 * trace_writer_mark_correlation_by_id — so the per-crossing call does no
 * string lookup, no interning and no allocation. If the string form were
 * primary each recorder would grow its own label cache and they would drift.
 */
int trace_writer_ensure_marker_id(trace_writer_t handle,
    const uint8_t* label, size_t label_len,
    uint64_t* out_id);

/*
 * Declare a boundary crossing against an already-interned label id.
 *
 * key_text / show_text are the NAMES the two values were read under.
 * show_text is load-bearing rather than cosmetic: a cross-process origin
 * chain resumes its walk on that name in the sending recording, so a marker
 * that drops it is visible with its history unreachable. Pass empty for the
 * defaults ("key", and "show" when a show_value is present).
 */
int trace_writer_mark_correlation_by_id(trace_writer_t handle,
    uint64_t marker_id,
    const uint8_t* boundary_label, size_t boundary_label_len,
    const uint8_t* direction, size_t direction_len,
    const uint8_t* key_value, size_t key_value_len,
    const uint8_t* show_value, size_t show_value_len,
    const uint8_t* description, size_t description_len,
    const uint8_t* key_text, size_t key_text_len,
    const uint8_t* show_text, size_t show_text_len);

/* Convenience wrapper: interns boundary_id, then forwards to _by_id. */
int trace_writer_mark_correlation(trace_writer_t handle,
    const uint8_t* direction, size_t direction_len,
    const uint8_t* boundary_id, size_t boundary_id_len,
    const uint8_t* key_value, size_t key_value_len,
    const uint8_t* show_value, size_t show_value_len,
    const uint8_t* description, size_t description_len,
    const uint8_t* key_text, size_t key_text_len,
    const uint8_t* show_text, size_t show_text_len);

/*
 * Declare that this recording covers a distributed-trace span, so a consumer
 * holding an OTel (trace_id, span_id) can decide that with one index lookup
 * instead of downloading and decoding the recording.
 *
 * trace_id is the 16 WIRE bytes and span_id the 8 WIRE bytes — NOT a hex
 * rendering. The index keys on the wire bytes, so passing hex here builds an
 * index keyed on something no consumer computes: present, correct-looking and
 * permanently unqueryable. Use the _hex form below when the host's OTel API
 * hands you hex; it is a wrapper over this one, so the conversion has a
 * single implementation rather than one per recorder.
 *
 * This mints no marker payload and no I/O event: a span-coverage marker has
 * no send/recv sense and no pairing domain, so forcing it into one would make
 * the pairing index try to pair spans with each other.
 */
int trace_writer_mark_span_coverage(trace_writer_t handle,
    const uint8_t* trace_id, size_t trace_id_len,
    const uint8_t* span_id, size_t span_id_len,
    uint64_t wall_time_unix_ns,
    uint64_t monotonic_time_ns);

/* Hex form: 32 hex characters for trace_id, 16 for span_id, either case. */
int trace_writer_mark_span_coverage_hex(trace_writer_t handle,
    const uint8_t* trace_id_hex, size_t trace_id_hex_len,
    const uint8_t* span_id_hex, size_t span_id_hex_len,
    uint64_t wall_time_unix_ns,
    uint64_t monotonic_time_ns);

/*
 * Open a native<->VM crossing span (Mixed-Trace-Debugging.md §3) and return its
 * minted span_id — the handle to pass to trace_writer_end_crossing.  The
 * crossing's start_step is the index of the first materialized step inside the
 * VM frame; the buffered pending step is flushed first (as
 * trace_writer_register_call does) so a crossing wrapping a call gets the same
 * start_step the call gets as its entryStep.  span_type names the crossing kind
 * (e.g. "gdscript-frame"), discriminated like "web-request" / "process".
 *
 * Streaming correctness (nested-trace-correlation.md §1.4): this call emits an
 * OPEN span record (flags.open, end_step = 0) and flushes it immediately, so the
 * in-flight crossing is visible to a reader before trace_writer_end_crossing
 * settles it with the same span_id (last-record-wins).
 *
 * Crossings index the materialized step space, so ONLY the multi-stream backend
 * supports them.  Returns 0 (never a valid 1-based span id) on any error — NULL
 * handle, a non-multi-stream backend, a not-ready or closed writer, or a failure
 * to write the open record — with trace_writer_last_error set.
 */
uint64_t trace_writer_begin_crossing(trace_writer_t handle,
    const char* span_type);

/*
 * Settle the crossing opened as span_id: its SpanRecord is written (same span_id
 * as the open record begin emitted, last-record-wins) with end_step = the last
 * materialized step inside the frame, then the span stream is flushed so the
 * record is committed to the container immediately (mid-run visibility, §3).  The
 * pending step is flushed first (as trace_writer_register_return does).
 * Crossings close strictly LIFO: span_id must be the innermost still-open
 * crossing.  Returns 0 on success, non-zero (with trace_writer_last_error set) on
 * a NULL handle, a non-multi-stream backend, a not-ready writer, or a span_id
 * that is not the innermost open crossing.
 */
int trace_writer_end_crossing(trace_writer_t handle, uint64_t span_id);

/*
 * Buffer an alternate source view for `path_id` (deminification support; spec
 * "Alternate Source Views", codetracer-trace-format-spec/internal-files.md).
 * `path_id` must already be registered. `view_kind`: 0 = raw, 1 = prettier_format,
 * 2 = black_format, 3-127 reserved, 128+ vendor-specific. `view_name` need not be
 * NUL-terminated (pass `view_name_len`). `content` is the formatted source bytes;
 * `sourcemap` is Sourcemap V3 JSON bytes (may be NULL/empty for "no sourcemap").
 * Multi-stream backend only. Returns the new view's 0-based index, or -1 on error
 * (with last_error set) — a signed return distinguishes index 0 from an error.
 */
int64_t trace_writer_register_source_view(trace_writer_t handle,
                                          uint64_t path_id,
                                          uint8_t view_kind,
                                          const char* view_name,
                                          size_t view_name_len,
                                          const uint8_t* content,
                                          size_t content_len,
                                          const uint8_t* sourcemap,
                                          size_t sourcemap_len);

/*
 * The exec-stream index the NEXT event registered on this writer will occupy —
 * the `start_step` a span opened right now should carry.  A span that runs from
 * here to there is `start_step = trace_writer_next_step_index()` at entry and
 * `end_step = trace_writer_next_step_index() - 1` at exit (clamped to
 * `start_step` when nothing was recorded in between).
 *
 * This is the writer's own step counter, NOT a count of
 * trace_writer_register_step calls: the counter advances for every exec-stream
 * event (absolute steps, DeltaColumn column moves, raise / catch, thread
 * start / exit / switch), and that counter is the step id every reader walks
 * (ct_reader_step(n), a span's start_step / end_step, the Request Panel's
 * startGeid).  A recorder counting its own register_step calls would drift the
 * moment it emitted a column delta or a thread event.
 *
 * Returns 0 when nothing has been recorded (NULL handle, non-multi-stream
 * backend, or a writer that has not begun events).
 */
uint64_t trace_writer_next_step_index(trace_writer_t handle);

/*
 * Decode the span stream of the `.ct` container at `path` into a JSON array —
 * the READ counterpart of trace_writer_register_span, so a recorder's own test
 * suite can assert on the spans it wrote through the canonical Nim decoder
 * instead of re-implementing one.
 *
 * `settled != 0` applies last-record-wins per span_id and sorts ascending by
 * span_id (what a panel displays); `settled == 0` returns every record in
 * append order, open records included (what a test asserting in-flight
 * publication needs).  Field names are the spec's wire names; `metadata` is an
 * ARRAY of [key, value] pairs because metadata ORDER is part of the contract.
 *
 * Returns NULL with *out_len = 0 on failure (see trace_writer_last_error); an
 * empty stream is the two-byte document "[]".  Free with ct_free_buffer.
 */
uint8_t* ct_spans_json(const char* path, int settled, size_t* out_len);

/* --------------------------------------------------------------------------
 * Thread lifecycle events
 *
 * Recorders that observe multi-threaded program execution emit ThreadStart /
 * ThreadExit / ThreadSwitch through these entry points.  Earlier versions of
 * the Nim backend dropped these events when they came in via the Rust shim's
 * ``TraceWriter::add_event(TraceLowLevelEvent::ThreadStart{,Exit,Switch})``
 * dispatch — see incidents 1.21 / 1.22 / 1.27.
 * -------------------------------------------------------------------------- */

void trace_writer_register_thread_start(trace_writer_t handle, uint64_t thread_id);
void trace_writer_register_thread_exit(trace_writer_t handle, uint64_t thread_id);
void trace_writer_register_thread_switch(trace_writer_t handle, uint64_t thread_id);

/* --------------------------------------------------------------------------
 * meta.dat — write via trace writer handle
 * -------------------------------------------------------------------------- */

int ct_write_meta_dat(trace_writer_t handle,
                      const uint8_t* recorder_id, size_t recorder_id_len);

/* --------------------------------------------------------------------------
 * meta.dat — standalone buffer write
 * -------------------------------------------------------------------------- */

/* M-REC-1: recording_id is the canonical UUIDv7 identity (RFC 9562)
 * for this recording.  Pass canonical lowercase hyphenated 36-char
 * form, or NULL/0 to have the writer mint one via the OS CSPRNG. */
int ct_write_meta_dat_to_buffer(
    const uint8_t* program, size_t program_len,
    const uint8_t* workdir, size_t workdir_len,
    const uint8_t* const* args, const size_t* arg_lens, size_t args_count,
    const uint8_t* const* paths, const size_t* path_lens, size_t paths_count,
    const uint8_t* recorder_id, size_t recorder_id_len,
    const uint8_t* recording_id, size_t recording_id_len,
    uint8_t** out_buf, size_t* out_len);

void ct_free_buffer(uint8_t* buf);

/* --------------------------------------------------------------------------
 * CTFS container — internal files added after the container was closed
 *
 * Every other writer entry point above operates on a `trace_writer_t`, i.e.
 * on a container the caller is still building.  These two work on a container
 * **on disk that has already been closed**, which is why they take a path:
 * there is no live writer to hand a handle for.
 *
 * They exist for producers of *derived* streams — data computed from a
 * finished trace that, by its own specification, must live inside the same
 * `.ct` rather than beside it.  Such a producer only knows what it wants to
 * store after the trace writer has sealed the file.
 *
 * Both return 0 on success and non-zero on failure; the reason is available
 * from trace_writer_last_error().
 * -------------------------------------------------------------------------- */

/* Write a new, empty CTFS v4 container at `path`.
 * `block_size = 0` selects the default of 4096. */
int ct_container_create(const char* path, uint32_t block_size);

/* Append `count` internal files to the already-closed container at `path`.
 *
 * names[i]     NUL-terminated internal filename; at most twelve characters
 *              from [0-9a-z./-] (CTFS base40, see CTFS-Binary-Format.md §3).
 * contents[i]  the file's complete content; may be NULL when lengths[i] == 0.
 * lengths[i]   its length in bytes.
 *
 * The container must be quiescent (no other writer), unencrypted, v4, and a
 * whole number of blocks.  A name that already exists is refused: CTFS is
 * append-only and this call will not overwrite a stream.
 *
 * The batch is published as a unit.  All new data and mapping blocks are
 * written and flushed first; the single rewrite of block 0 that makes them
 * reachable happens last.  A crash in between leaves unreferenced trailing
 * blocks — wasteful, still readable — never an entry pointing at absent data.
 * There is deliberately no singular form of this call: attaching a related
 * set of streams one at a time would make a half-attached container
 * reachable, and every reader would have to cope with it. */
int ct_container_append_files(const char* path,
                              const char* const* names,
                              const uint8_t* const* contents,
                              const size_t* lengths,
                              size_t count);

/* --------------------------------------------------------------------------
 * meta.dat — reader handle
 * -------------------------------------------------------------------------- */

typedef void* meta_dat_reader_t;

meta_dat_reader_t ct_read_meta_dat(const uint8_t* data, size_t len);
/* M-REC-1: returns the UUIDv7 recording_id; pointer valid until
 * ct_meta_dat_free. */
const uint8_t* ct_meta_dat_recording_id(meta_dat_reader_t h, size_t* out_len);
const uint8_t* ct_meta_dat_program(meta_dat_reader_t h, size_t* out_len);
const uint8_t* ct_meta_dat_workdir(meta_dat_reader_t h, size_t* out_len);
size_t ct_meta_dat_args_count(meta_dat_reader_t h);
const uint8_t* ct_meta_dat_arg(meta_dat_reader_t h, size_t idx, size_t* out_len);
size_t ct_meta_dat_paths_count(meta_dat_reader_t h);
const uint8_t* ct_meta_dat_path(meta_dat_reader_t h, size_t idx, size_t* out_len);
const uint8_t* ct_meta_dat_recorder_id(meta_dat_reader_t h, size_t* out_len);
void ct_meta_dat_free(meta_dat_reader_t h);

/* --------------------------------------------------------------------------
 * Streaming value encoder (zero-allocation CBOR)
 * -------------------------------------------------------------------------- */

typedef void* value_encoder_t;

value_encoder_t ct_value_encoder_new(void);
void ct_value_encoder_free(value_encoder_t h);
void ct_value_encoder_reset(value_encoder_t h);

int ct_value_write_int(value_encoder_t h, int64_t value, uint64_t type_id);
int ct_value_write_float(value_encoder_t h, double value, uint64_t type_id);
int ct_value_write_bool(value_encoder_t h, int value);
int ct_value_write_bool_typed(value_encoder_t h, int value, uint64_t type_id);
int ct_value_write_string(value_encoder_t h, const uint8_t* data, size_t len, uint64_t type_id);
int ct_value_write_none(value_encoder_t h);
int ct_value_write_none_typed(value_encoder_t h, uint64_t type_id);
int ct_value_write_raw(value_encoder_t h, const uint8_t* data, size_t len, uint64_t type_id);
int ct_value_write_error(value_encoder_t h, const uint8_t* data, size_t len, uint64_t type_id);

int ct_value_begin_struct(value_encoder_t h, uint64_t type_id, int field_count);
int ct_value_begin_sequence(value_encoder_t h, uint64_t type_id, int element_count);
int ct_value_begin_tuple(value_encoder_t h, uint64_t type_id, int element_count);
int ct_value_begin_variant(value_encoder_t h, const uint8_t* discriminator, size_t disc_len, uint64_t type_id);
int ct_value_begin_reference(value_encoder_t h, uint64_t address, int mutable, uint64_t type_id);
int ct_value_end_compound(value_encoder_t h);

int ct_value_write_char(value_encoder_t h, uint32_t codepoint, uint64_t type_id);
int ct_value_write_bigint(value_encoder_t h, const uint8_t* data, size_t len, int negative, uint64_t type_id);

const uint8_t* ct_value_get_bytes(value_encoder_t h, size_t* out_len);

#ifdef __cplusplus
}
#endif

#endif /* CODETRACER_TRACE_WRITER_H */
