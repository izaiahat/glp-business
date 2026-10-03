#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef F13_PYTHON_PATH
#define F13_PYTHON_PATH "/usr/bin/python3.12"
#endif
#ifndef F13_PYTHON_SHA256
#define F13_PYTHON_SHA256 "e50d468e8b0adfb05733f5b87b3cff34829c4a8c1aea50c865aa8bdfe4bb150f"
#endif
#ifndef F13_PYTHON_UID
#define F13_PYTHON_UID 0
#endif
#ifndef F13_PYTHON_GID
#define F13_PYTHON_GID 0
#endif
#ifndef F13_PYTHON_MODE
#define F13_PYTHON_MODE 0755
#endif
#ifndef F13_PYTHON_NLINK
#define F13_PYTHON_NLINK 1
#endif

#ifndef F13_PAYLOAD_PATH
#define F13_PAYLOAD_PATH "/home/ubuntu/business/scripts/f13_atop_retention.py"
#endif
#ifndef F13_PAYLOAD_SHA256
#define F13_PAYLOAD_SHA256 "6b32506fce5442dd8017568ccf6f5af0bf1e624649b6dd0b8a8110eaca5da4e1"
#endif
#ifndef F13_PAYLOAD_UID
#define F13_PAYLOAD_UID 1000
#endif
#ifndef F13_PAYLOAD_GID
#define F13_PAYLOAD_GID 1000
#endif
#ifndef F13_PAYLOAD_MODE
#define F13_PAYLOAD_MODE 0644
#endif
#ifndef F13_PAYLOAD_NLINK
#define F13_PAYLOAD_NLINK 1
#endif

#ifndef AT_EMPTY_PATH
#define AT_EMPTY_PATH 0x1000
#endif

#define SHA256_BLOCK_SIZE 64U
#define SHA256_DIGEST_SIZE 32U
#define HASH_BUFFER_SIZE 65536U
#define MIN_EXEC_FD 64

struct sha256_context {
    uint8_t data[SHA256_BLOCK_SIZE];
    uint32_t state[8];
    uint64_t bit_length;
    size_t data_length;
};

struct file_contract {
    const char *label;
    const char *path;
    const char *sha256_hex;
    uid_t uid;
    gid_t gid;
    mode_t mode;
    nlink_t nlink;
};

struct verified_file {
    int descriptor;
    struct stat identity;
    char sha256_hex[(SHA256_DIGEST_SIZE * 2U) + 1U];
};

static int failf(const char *format, ...)
{
    va_list args;

    (void)fprintf(stderr, "f13-atop-retention-launcher: ");
    va_start(args, format);
    (void)vfprintf(stderr, format, args);
    va_end(args);
    (void)fputc('\n', stderr);
    return 126;
}

static uint32_t rotate_right(uint32_t value, uint32_t count)
{
    return (value >> count) | (value << (32U - count));
}

static uint32_t choose(uint32_t x, uint32_t y, uint32_t z)
{
    return (x & y) ^ ((~x) & z);
}

static uint32_t majority(uint32_t x, uint32_t y, uint32_t z)
{
    return (x & y) ^ (x & z) ^ (y & z);
}

static uint32_t sigma0(uint32_t x)
{
    return rotate_right(x, 2U) ^ rotate_right(x, 13U) ^ rotate_right(x, 22U);
}

static uint32_t sigma1(uint32_t x)
{
    return rotate_right(x, 6U) ^ rotate_right(x, 11U) ^ rotate_right(x, 25U);
}

static uint32_t gamma0(uint32_t x)
{
    return rotate_right(x, 7U) ^ rotate_right(x, 18U) ^ (x >> 3U);
}

static uint32_t gamma1(uint32_t x)
{
    return rotate_right(x, 17U) ^ rotate_right(x, 19U) ^ (x >> 10U);
}

static void sha256_transform(struct sha256_context *context, const uint8_t data[SHA256_BLOCK_SIZE])
{
    static const uint32_t constants[64] = {
        0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
        0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
        0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
        0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
        0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
        0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
        0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
        0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
        0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
        0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
        0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
        0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
        0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
        0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
        0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
        0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U
    };
    uint32_t words[64];
    uint32_t a;
    uint32_t b;
    uint32_t c;
    uint32_t d;
    uint32_t e;
    uint32_t f;
    uint32_t g;
    uint32_t h;
    size_t index;

    for (index = 0U; index < 16U; ++index) {
        size_t offset = index * 4U;
        words[index] = ((uint32_t)data[offset] << 24U)
                     | ((uint32_t)data[offset + 1U] << 16U)
                     | ((uint32_t)data[offset + 2U] << 8U)
                     | (uint32_t)data[offset + 3U];
    }
    for (index = 16U; index < 64U; ++index) {
        words[index] = gamma1(words[index - 2U]) + words[index - 7U]
                     + gamma0(words[index - 15U]) + words[index - 16U];
    }

    a = context->state[0];
    b = context->state[1];
    c = context->state[2];
    d = context->state[3];
    e = context->state[4];
    f = context->state[5];
    g = context->state[6];
    h = context->state[7];

    for (index = 0U; index < 64U; ++index) {
        uint32_t first = h + sigma1(e) + choose(e, f, g) + constants[index] + words[index];
        uint32_t second = sigma0(a) + majority(a, b, c);
        h = g;
        g = f;
        f = e;
        e = d + first;
        d = c;
        c = b;
        b = a;
        a = first + second;
    }

    context->state[0] += a;
    context->state[1] += b;
    context->state[2] += c;
    context->state[3] += d;
    context->state[4] += e;
    context->state[5] += f;
    context->state[6] += g;
    context->state[7] += h;
}

static void sha256_init(struct sha256_context *context)
{
    context->data_length = 0U;
    context->bit_length = 0U;
    context->state[0] = 0x6a09e667U;
    context->state[1] = 0xbb67ae85U;
    context->state[2] = 0x3c6ef372U;
    context->state[3] = 0xa54ff53aU;
    context->state[4] = 0x510e527fU;
    context->state[5] = 0x9b05688cU;
    context->state[6] = 0x1f83d9abU;
    context->state[7] = 0x5be0cd19U;
}

static void sha256_update(struct sha256_context *context, const uint8_t *data, size_t length)
{
    size_t index;

    for (index = 0U; index < length; ++index) {
        context->data[context->data_length] = data[index];
        ++context->data_length;
        if (context->data_length == SHA256_BLOCK_SIZE) {
            sha256_transform(context, context->data);
            context->bit_length += 512U;
            context->data_length = 0U;
        }
    }
}

static void sha256_final(struct sha256_context *context, uint8_t digest[SHA256_DIGEST_SIZE])
{
    size_t index = context->data_length;
    size_t state_index;

    context->data[index++] = 0x80U;
    if (index > 56U) {
        while (index < SHA256_BLOCK_SIZE) {
            context->data[index++] = 0U;
        }
        sha256_transform(context, context->data);
        index = 0U;
    }
    while (index < 56U) {
        context->data[index++] = 0U;
    }

    context->bit_length += (uint64_t)context->data_length * 8U;
    context->data[63] = (uint8_t)context->bit_length;
    context->data[62] = (uint8_t)(context->bit_length >> 8U);
    context->data[61] = (uint8_t)(context->bit_length >> 16U);
    context->data[60] = (uint8_t)(context->bit_length >> 24U);
    context->data[59] = (uint8_t)(context->bit_length >> 32U);
    context->data[58] = (uint8_t)(context->bit_length >> 40U);
    context->data[57] = (uint8_t)(context->bit_length >> 48U);
    context->data[56] = (uint8_t)(context->bit_length >> 56U);
    sha256_transform(context, context->data);

    for (state_index = 0U; state_index < 4U; ++state_index) {
        digest[state_index] = (uint8_t)(context->state[0] >> (24U - (state_index * 8U)));
        digest[state_index + 4U] = (uint8_t)(context->state[1] >> (24U - (state_index * 8U)));
        digest[state_index + 8U] = (uint8_t)(context->state[2] >> (24U - (state_index * 8U)));
        digest[state_index + 12U] = (uint8_t)(context->state[3] >> (24U - (state_index * 8U)));
        digest[state_index + 16U] = (uint8_t)(context->state[4] >> (24U - (state_index * 8U)));
        digest[state_index + 20U] = (uint8_t)(context->state[5] >> (24U - (state_index * 8U)));
        digest[state_index + 24U] = (uint8_t)(context->state[6] >> (24U - (state_index * 8U)));
        digest[state_index + 28U] = (uint8_t)(context->state[7] >> (24U - (state_index * 8U)));
    }
}

static void digest_to_hex(const uint8_t digest[SHA256_DIGEST_SIZE], char output[(SHA256_DIGEST_SIZE * 2U) + 1U])
{
    static const char alphabet[] = "0123456789abcdef";
    size_t index;

    for (index = 0U; index < SHA256_DIGEST_SIZE; ++index) {
        output[index * 2U] = alphabet[digest[index] >> 4U];
        output[(index * 2U) + 1U] = alphabet[digest[index] & 0x0fU];
    }
    output[SHA256_DIGEST_SIZE * 2U] = '\0';
}

static bool same_identity(const struct stat *left, const struct stat *right)
{
    return left->st_dev == right->st_dev
        && left->st_ino == right->st_ino
        && left->st_mode == right->st_mode
        && left->st_uid == right->st_uid
        && left->st_gid == right->st_gid
        && left->st_nlink == right->st_nlink
        && left->st_size == right->st_size
        && left->st_mtim.tv_sec == right->st_mtim.tv_sec
        && left->st_mtim.tv_nsec == right->st_mtim.tv_nsec
        && left->st_ctim.tv_sec == right->st_ctim.tv_sec
        && left->st_ctim.tv_nsec == right->st_ctim.tv_nsec;
}

static bool valid_component(const char *component)
{
    return component[0] != '\0'
        && strcmp(component, ".") != 0
        && strcmp(component, "..") != 0;
}

static int open_canonical_readonly(const char *path)
{
    char copy[PATH_MAX];
    char *component;
    int directory_fd;

    if (path == NULL || path[0] != '/' || path[1] == '\0' || strlen(path) >= sizeof(copy)) {
        errno = EINVAL;
        return -1;
    }
    (void)memcpy(copy, path, strlen(path) + 1U);
    if (copy[strlen(copy) - 1U] == '/') {
        errno = EINVAL;
        return -1;
    }

    directory_fd = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (directory_fd < 0) {
        return -1;
    }

    component = copy + 1;
    for (;;) {
        char *separator = strchr(component, '/');
        int next_fd;

        if (separator != NULL) {
            *separator = '\0';
        }
        if (!valid_component(component)) {
            (void)close(directory_fd);
            errno = EINVAL;
            return -1;
        }
        if (separator == NULL) {
            next_fd = openat(directory_fd, component, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
            (void)close(directory_fd);
            return next_fd;
        }
        next_fd = openat(directory_fd, component,
                         O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        if (next_fd < 0) {
            int saved_errno = errno;
            (void)close(directory_fd);
            errno = saved_errno;
            return -1;
        }
        (void)close(directory_fd);
        directory_fd = next_fd;
        component = separator + 1;
    }
}

static int validate_contract(const struct file_contract *contract, const struct stat *identity)
{
    if (!S_ISREG(identity->st_mode)) {
        return failf("%s is not a regular file", contract->label);
    }
    if (identity->st_uid != contract->uid || identity->st_gid != contract->gid) {
        return failf("%s owner mismatch", contract->label);
    }
    if ((identity->st_mode & 07777U) != contract->mode) {
        return failf("%s mode mismatch", contract->label);
    }
    if (identity->st_nlink != contract->nlink) {
        return failf("%s link-count mismatch", contract->label);
    }
    if (identity->st_size < 0) {
        return failf("%s negative size", contract->label);
    }
    return 0;
}

#if defined(F13_ENABLE_TEST_HOOKS)
static int test_hook_fd(const char *name)
{
    const char *text = getenv(name);
    char *end = NULL;
    long parsed;

    if (text == NULL || text[0] == '\0') {
        return -1;
    }
    errno = 0;
    parsed = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed < 0L || parsed > INT_MAX) {
        return -1;
    }
    return (int)parsed;
}

static int run_test_hook(const char *stage)
{
    const char *wanted = getenv("F13_TEST_HOOK_STAGE");
    int signal_fd;
    int acknowledge_fd;
    uint8_t byte = 0x46U;

    if (wanted == NULL || strcmp(wanted, stage) != 0) {
        return 0;
    }
    signal_fd = test_hook_fd("F13_TEST_HOOK_SIGNAL_FD");
    acknowledge_fd = test_hook_fd("F13_TEST_HOOK_ACK_FD");
    if (signal_fd < 0 || acknowledge_fd < 0) {
        return failf("test hook descriptors invalid");
    }
    if (write(signal_fd, &byte, 1U) != 1) {
        return failf("test hook signal failed");
    }
    if (read(acknowledge_fd, &byte, 1U) != 1) {
        return failf("test hook acknowledgement failed");
    }
    return 0;
}
#else
static int run_test_hook(const char *stage)
{
    (void)stage;
    return 0;
}
#endif

static int hash_descriptor(int descriptor, off_t size, char output[(SHA256_DIGEST_SIZE * 2U) + 1U])
{
    struct sha256_context context;
    uint8_t buffer[HASH_BUFFER_SIZE];
    off_t offset = 0;
    uint8_t digest[SHA256_DIGEST_SIZE];

    sha256_init(&context);
    while (offset < size) {
        off_t remaining = size - offset;
        size_t wanted = remaining < (off_t)sizeof(buffer) ? (size_t)remaining : sizeof(buffer);
        ssize_t amount;

        do {
            amount = pread(descriptor, buffer, wanted, offset);
        } while (amount < 0 && errno == EINTR);
        if (amount <= 0) {
            return failf("descriptor hash read failed");
        }
        sha256_update(&context, buffer, (size_t)amount);
        offset += amount;
    }
    for (;;) {
        ssize_t extra = pread(descriptor, buffer, 1U, size);
        if (extra < 0 && errno == EINTR) {
            continue;
        }
        if (extra < 0) {
            return failf("descriptor growth check failed");
        }
        if (extra != 0) {
            return failf("descriptor grew while hashing");
        }
        break;
    }
    sha256_final(&context, digest);
    digest_to_hex(digest, output);
    return 0;
}

static int verify_path_binding(const struct file_contract *contract, const struct stat *expected)
{
    int descriptor = open_canonical_readonly(contract->path);
    struct stat observed;

    if (descriptor < 0) {
        return failf("%s canonical path re-open failed: %s", contract->label, strerror(errno));
    }
    if (fstat(descriptor, &observed) != 0) {
        int saved_errno = errno;
        (void)close(descriptor);
        return failf("%s path identity unavailable: %s", contract->label, strerror(saved_errno));
    }
    (void)close(descriptor);
    if (!same_identity(expected, &observed)) {
        return failf("%s path/descriptor identity drift", contract->label);
    }
    return 0;
}

static int verify_file(const struct file_contract *contract, struct verified_file *result)
{
    struct stat before;
    struct stat after;
    char hook_stage[64];
    int hook_result;
    int contract_result;

    result->descriptor = open_canonical_readonly(contract->path);
    if (result->descriptor < 0) {
        return failf("%s open failed: %s", contract->label, strerror(errno));
    }
    if (fstat(result->descriptor, &before) != 0) {
        int saved_errno = errno;
        (void)close(result->descriptor);
        result->descriptor = -1;
        return failf("%s initial fstat failed: %s", contract->label, strerror(saved_errno));
    }
    contract_result = validate_contract(contract, &before);
    if (contract_result != 0) {
        (void)close(result->descriptor);
        result->descriptor = -1;
        return contract_result;
    }
    if (hash_descriptor(result->descriptor, before.st_size, result->sha256_hex) != 0) {
        (void)close(result->descriptor);
        result->descriptor = -1;
        return 126;
    }
    if (snprintf(hook_stage, sizeof(hook_stage), "%s_after_hash", contract->label)
            >= (int)sizeof(hook_stage)) {
        (void)close(result->descriptor);
        result->descriptor = -1;
        return failf("internal hook label overflow");
    }
    hook_result = run_test_hook(hook_stage);
    if (hook_result != 0) {
        (void)close(result->descriptor);
        result->descriptor = -1;
        return hook_result;
    }
    if (fstat(result->descriptor, &after) != 0) {
        int saved_errno = errno;
        (void)close(result->descriptor);
        result->descriptor = -1;
        return failf("%s final fstat failed: %s", contract->label, strerror(saved_errno));
    }
    if (!same_identity(&before, &after)) {
        (void)close(result->descriptor);
        result->descriptor = -1;
        return failf("%s identity/size/mtime/ctime drift while hashing", contract->label);
    }
    if (strcmp(result->sha256_hex, contract->sha256_hex) != 0) {
        (void)close(result->descriptor);
        result->descriptor = -1;
        return failf("%s SHA-256 mismatch", contract->label);
    }
    if (verify_path_binding(contract, &before) != 0) {
        (void)close(result->descriptor);
        result->descriptor = -1;
        return 126;
    }
    result->identity = before;
    return 0;
}

static int move_descriptor(int descriptor, int minimum)
{
    int moved = fcntl(descriptor, F_DUPFD_CLOEXEC, minimum);
    int saved_errno = errno;

    (void)close(descriptor);
    errno = saved_errno;
    return moved;
}

static int verify_procfd_binding(int descriptor, char *path, size_t path_size)
{
    struct stat retained;
    struct stat observed;
    int written = snprintf(path, path_size, "/proc/self/fd/%d", descriptor);

    if (written < 0 || (size_t)written >= path_size) {
        return failf("procfd path overflow");
    }
    if (fstat(descriptor, &retained) != 0 || stat(path, &observed) != 0) {
        return failf("procfd resolution failed: %s", strerror(errno));
    }
    if (!same_identity(&retained, &observed)) {
        return failf("procfd did not resolve to retained payload object");
    }
    return 0;
}

static bool same_payload_authority_except_ctime(const struct stat *left, const struct stat *right)
{
    return left->st_dev == right->st_dev
        && left->st_ino == right->st_ino
        && left->st_mode == right->st_mode
        && left->st_uid == right->st_uid
        && left->st_gid == right->st_gid
        && left->st_nlink == right->st_nlink
        && left->st_size == right->st_size
        && left->st_mtim.tv_sec == right->st_mtim.tv_sec
        && left->st_mtim.tv_nsec == right->st_mtim.tv_nsec;
}

static int revalidate_retained_payload(const struct file_contract *contract,
                                       const struct verified_file *file)
{
    struct stat before;
    struct stat after;
    char digest[(SHA256_DIGEST_SIZE * 2U) + 1U];

    if (fstat(file->descriptor, &before) != 0) {
        return failf("retained payload fstat failed: %s", strerror(errno));
    }
    /* A namespace-only rename updates ctime. Re-admit every other identity
     * field and exact bytes from the retained descriptor, so a final path
     * replacement cannot force a pathname reopen while byte/metadata drift is
     * still rejected immediately adjacent to exec. */
    if (!same_payload_authority_except_ctime(&file->identity, &before)) {
        return failf("retained payload authority drift before exec");
    }
    if (validate_contract(contract, &before) != 0) {
        return 126;
    }
    if (hash_descriptor(file->descriptor, before.st_size, digest) != 0) {
        return 126;
    }
    if (strcmp(digest, contract->sha256_hex) != 0) {
        return failf("retained payload SHA-256 drift before exec");
    }
    if (fstat(file->descriptor, &after) != 0 || !same_identity(&before, &after)) {
        return failf("retained payload identity/size/mtime/ctime drift during final hash");
    }
    return 0;
}

int main(int argc, char **argv)
{
    static const struct file_contract python_contract = {
        "python", F13_PYTHON_PATH, F13_PYTHON_SHA256,
        (uid_t)F13_PYTHON_UID, (gid_t)F13_PYTHON_GID,
        (mode_t)F13_PYTHON_MODE, (nlink_t)F13_PYTHON_NLINK
    };
    static const struct file_contract payload_contract = {
        "payload", F13_PAYLOAD_PATH, F13_PAYLOAD_SHA256,
        (uid_t)F13_PAYLOAD_UID, (gid_t)F13_PAYLOAD_GID,
        (mode_t)F13_PAYLOAD_MODE, (nlink_t)F13_PAYLOAD_NLINK
    };
    struct verified_file python_file = {.descriptor = -1};
    struct verified_file payload_file = {.descriptor = -1};
    const char *mode;
    char payload_procfd[64];
    char *child_argv[6];
    char *child_env[] = {
        "AWS_EC2_METADATA_DISABLED=true",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONNOUSERSITE=1",
        NULL
    };
    int flags;
    int error_number;

    if (argc != 2 || argv[1] == NULL) {
        return failf("accepts exactly one mode: --dry-run or --live");
    }
    if (strcmp(argv[1], "--dry-run") == 0) {
        mode = "--dry-run";
    } else if (strcmp(argv[1], "--live") == 0) {
        if (geteuid() != (uid_t)0) {
            return failf("--live requires effective uid 0");
        }
        mode = "--live";
    } else {
        return failf("mode must be exactly --dry-run or --live");
    }

    if (verify_file(&python_contract, &python_file) != 0) {
        return 126;
    }
    if (verify_file(&payload_contract, &payload_file) != 0) {
        (void)close(python_file.descriptor);
        return 126;
    }

    python_file.descriptor = move_descriptor(python_file.descriptor, MIN_EXEC_FD);
    if (python_file.descriptor < 0) {
        (void)close(payload_file.descriptor);
        return failf("python descriptor relocation failed: %s", strerror(errno));
    }
    payload_file.descriptor = move_descriptor(payload_file.descriptor, MIN_EXEC_FD);
    if (payload_file.descriptor < 0) {
        (void)close(python_file.descriptor);
        return failf("payload descriptor relocation failed: %s", strerror(errno));
    }
    if (python_file.descriptor == payload_file.descriptor
            || python_file.descriptor < MIN_EXEC_FD
            || payload_file.descriptor < MIN_EXEC_FD) {
        (void)close(python_file.descriptor);
        (void)close(payload_file.descriptor);
        return failf("descriptor collision or unsafe descriptor number");
    }

    if (verify_path_binding(&python_contract, &python_file.identity) != 0
            || verify_path_binding(&payload_contract, &payload_file.identity) != 0) {
        (void)close(python_file.descriptor);
        (void)close(payload_file.descriptor);
        return 126;
    }

    flags = fcntl(payload_file.descriptor, F_GETFD);
    if (flags < 0 || fcntl(payload_file.descriptor, F_SETFD, flags & ~FD_CLOEXEC) != 0) {
        error_number = errno;
        (void)close(python_file.descriptor);
        (void)close(payload_file.descriptor);
        return failf("payload descriptor inheritance setup failed: %s", strerror(error_number));
    }
    if (verify_procfd_binding(payload_file.descriptor,
                              payload_procfd, sizeof(payload_procfd)) != 0) {
        (void)close(python_file.descriptor);
        (void)close(payload_file.descriptor);
        return 126;
    }

    if (run_test_hook("before_exec") != 0) {
        (void)close(python_file.descriptor);
        (void)close(payload_file.descriptor);
        return 126;
    }
    if (revalidate_retained_payload(&payload_contract, &payload_file) != 0
            || verify_procfd_binding(payload_file.descriptor,
                                     payload_procfd, sizeof(payload_procfd)) != 0) {
        (void)close(python_file.descriptor);
        (void)close(payload_file.descriptor);
        return 126;
    }

    child_argv[0] = (char *)F13_PYTHON_PATH;
    child_argv[1] = (char *)"-I";
    child_argv[2] = (char *)"-S";
    child_argv[3] = payload_procfd;
    child_argv[4] = (char *)mode;
    child_argv[5] = NULL;

#if defined(SYS_execveat)
    (void)syscall(SYS_execveat, python_file.descriptor, "", child_argv, child_env, AT_EMPTY_PATH);
    if (errno != ENOSYS) {
        error_number = errno;
        (void)close(python_file.descriptor);
        (void)close(payload_file.descriptor);
        return failf("descriptor-bound execveat failed: %s", strerror(error_number));
    }
#endif
    fexecve(python_file.descriptor, child_argv, child_env);
    error_number = errno;
    (void)close(python_file.descriptor);
    (void)close(payload_file.descriptor);
    return failf("descriptor-bound fexecve failed: %s", strerror(error_number));
}
