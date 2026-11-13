package io.openontology.replica;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.ByteArrayInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InvalidClassException;
import java.io.ObjectInputFilter;
import java.io.ObjectInputStream;
import java.io.OptionalDataException;
import java.io.PrintStream;
import java.io.StreamCorruptedException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.TreeMap;

/**
 * Safely reads a Debezium offsets file ({@code HashMap<byte[], byte[]>} written by
 * {@code FileOffsetBackingStore}, keys/values being internal converter JSON) and prints
 * a single-line JSON array of {@code {"partition":{...},"offset":{...}}} entries on
 * stdout. Entries are deduplicated by canonical key JSON (last wins, matching
 * FileOffsetBackingStore load semantics) and sorted for deterministic output.
 *
 * <p>Two canonical key/value shapes are accepted, nothing broader:
 * <ol>
 *   <li><b>Debezium Server / embedded engine format</b> (observed on Debezium Server
 *       3.7.1.Final): key is the JSON array
 *       {@code ["<engine-name>", {"server":"...", ...}]} with a non-empty engine name
 *       and a partition object whose values are all scalars and which carries a
 *       non-empty string {@code server}; value is the offset map itself as a plain
 *       JSON object of scalar values. {@code {}} values (partition registered, no
 *       committed offset yet) are reported as an empty {@code offset} object so the
 *       partition is not dropped; the executor is expected to refuse an active resume
 *       on such entries, they are never silently treated as a resume position.</li>
 *   <li><b>Kafka Connect worker format</b>: key is
 *       {@code {"connector":...,"partition":{...}}} and value is
 *       {@code {"offset":{...}}} (either side optionally wrapped in a schemaful
 *       {@code {"schema":{...},"payload":{...}}} envelope).</li>
 * </ol>
 * All partition/offset map values must be scalars (string, number, boolean) in both
 * forms. Anything else is BAD_SHAPE.
 *
 * <p>Deserialization is guarded by an {@link ObjectInputFilter} that only allows
 * {@code java.util.HashMap}, {@code byte[]} and {@code java.util.Map$Entry;[]} (the
 * table array JDK deserialization allocates while rebuilding a HashMap) with
 * depth/reference/byte/array-length limits; any other class (e.g. deserialization
 * gadgets) is rejected before it is instantiated. {@link String} values bypass the
 * class filter (written as atomic {@code TC_STRING}), so every entry is additionally
 * verified to be {@code byte[] -> byte[]} after deserialization. No lenient byte
 * scanning is performed and error messages never echo source values: anything that
 * does not parse is an error, and truncation/retry decisions belong to the Python
 * caller via the exit code.
 *
 * <p>Exit codes:
 * <ul>
 *   <li>0 OK (a missing or empty file prints {@code []}, like FileOffsetBackingStore)</li>
 *   <li>2 usage error</li>
 *   <li>3 REJECTED: disallowed class, non byte[] entries, oversized input, trailing data</li>
 *   <li>4 TRUNCATED/CORRUPT stream (retry decision belongs to the caller)</li>
 *   <li>5 BAD_SHAPE: entries are not one of the two expected internal JSON shapes</li>
 * </ul>
 */
public final class ReadOffsets {

    public static final int EXIT_OK = 0;
    public static final int EXIT_USAGE = 2;
    public static final int EXIT_REJECTED = 3;
    public static final int EXIT_TRUNCATED = 4;
    public static final int EXIT_BAD_SHAPE = 5;

    private static final long MAX_DEPTH = 8;
    private static final long MAX_REFS = 1_000_000;
    /** Visible to the self-test, which builds an over-limit file against it. */
    static final long MAX_BYTES = 64L * 1024 * 1024;
    private static final int MAX_ARRAY_LENGTH = 8 * 1024 * 1024;

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ReadOffsets() {
    }

    public static void main(String[] args) {
        if (args.length != 1) {
            System.err.println("usage: ReadOffsets <offsets-file>");
            System.exit(EXIT_USAGE);
        }
        int code = run(Path.of(args[0]), System.out, System.err);
        System.out.flush();
        System.exit(code);
    }

    static int run(Path file, PrintStream out, PrintStream err) {
        final byte[] fileBytes;
        try {
            if (!Files.exists(file)) {
                err.println("MISSING offset file " + file + ", treating as empty");
                write(out, "[]");
                return EXIT_OK;
            }
            long size = Files.size(file);
            if (size == 0) {
                write(out, "[]");
                return EXIT_OK;
            }
            if (size > MAX_BYTES) {
                err.println("REJECTED offset file " + file + " too large: " + size + " > " + MAX_BYTES + " bytes");
                return EXIT_REJECTED;
            }
            fileBytes = Files.readAllBytes(file);
        } catch (IOException e) {
            err.println("IO failure on " + file + ": " + e);
            return EXIT_TRUNCATED;
        }

        final Map<byte[], byte[]> raw;
        try {
            raw = deserializeOffsets(fileBytes);
        } catch (ShapeException e) {
            err.println("REJECTED malformed offsets content: " + e.getMessage());
            return EXIT_REJECTED;
        } catch (InvalidClassException e) {
            err.println("REJECTED disallowed serialized content: " + e.getMessage());
            return EXIT_REJECTED;
        } catch (EOFException | OptionalDataException e) {
            err.println("TRUNCATED offsets stream: " + e);
            return EXIT_TRUNCATED;
        } catch (StreamCorruptedException e) {
            err.println("CORRUPT offsets stream: " + e.getMessage());
            return EXIT_TRUNCATED;
        } catch (ClassNotFoundException | ClassCastException e) {
            err.println("REJECTED unexpected class in offsets stream: " + e);
            return EXIT_REJECTED;
        } catch (IOException e) {
            err.println("I/O failure deserializing offsets: " + e);
            return EXIT_TRUNCATED;
        }

        final String json;
        try {
            json = toJsonArray(raw);
        } catch (IOException e) {
            err.println("BAD_SHAPE offset entries are not in an expected internal JSON shape: " + e.getMessage());
            return EXIT_BAD_SHAPE;
        }
        write(out, json);
        return EXIT_OK;
    }

    /**
     * Signals content that deserialized without breaking the stream but is not the
     * expected shape (wrong root type, non {@code byte[]} entries, trailing data).
     */
    static final class ShapeException extends IOException {
        private static final long serialVersionUID = 1L;

        ShapeException(String message) {
            super(message);
        }
    }

    private static Map<byte[], byte[]> deserializeOffsets(byte[] fileBytes)
            throws IOException, ClassNotFoundException {
        ObjectInputFilter filter = info -> {
            final Class<?> serialClass = info.serialClass();
            if (serialClass != null
                    && serialClass != HashMap.class
                    && serialClass != byte[].class
                    && serialClass != Map.Entry[].class) {
                return ObjectInputFilter.Status.REJECTED;
            }
            if (info.arrayLength() >= 0 && info.arrayLength() > MAX_ARRAY_LENGTH) {
                return ObjectInputFilter.Status.REJECTED;
            }
            if (info.depth() > MAX_DEPTH
                    || info.references() > MAX_REFS
                    || info.streamBytes() > MAX_BYTES) {
                return ObjectInputFilter.Status.REJECTED;
            }
            return ObjectInputFilter.Status.ALLOWED;
        };
        try (ObjectInputStream in = new ObjectInputStream(new ByteArrayInputStream(fileBytes))) {
            in.setObjectInputFilter(filter);
            Object root = in.readObject();
            if (in.read() != -1) {
                throw new ShapeException("trailing data after offsets map");
            }
            if (!(root instanceof HashMap)) {
                throw new ShapeException(
                        "expected HashMap, got " + (root == null ? "null" : root.getClass().getName()));
            }
            @SuppressWarnings("unchecked")
            Map<byte[], byte[]> map = (Map<byte[], byte[]>) root;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof byte[]) || !(entry.getValue() instanceof byte[])) {
                    throw new ShapeException(
                            "offsets map entries must be byte[] -> byte[], found "
                                    + className(entry.getKey()) + " -> " + className(entry.getValue()));
                }
            }
            return map;
        }
    }

    private static String toJsonArray(Map<byte[], byte[]> raw) throws IOException {
        TreeMap<String, ObjectNode> ordered = new TreeMap<>();
        for (Map.Entry<byte[], byte[]> entry : raw.entrySet()) {
            JsonNode keyNode = readContainerNode(entry.getKey(), "key");
            JsonNode valueNode = readObjectNode(entry.getValue(), "value");
            JsonNode partition = parsePartition(keyNode);
            JsonNode offset = parseOffset(keyNode, valueNode);
            validateScalarValues(partition, "partition");
            validateScalarValues(offset, "offset");
            ObjectNode item = MAPPER.createObjectNode();
            item.set("partition", partition.deepCopy());
            item.set("offset", offset.deepCopy());
            ordered.put(MAPPER.writeValueAsString(keyNode), item);
        }
        ArrayNode array = MAPPER.createArrayNode();
        for (ObjectNode item : ordered.values()) {
            array.add(item);
        }
        return MAPPER.writeValueAsString(array);
    }

    /**
     * Engine format: {@code ["<engine>", {"server":"...", ...}]} with strict member
     * rules. Connect format: object with an object {@code partition} member. Any other
     * shape (including a JSON {@code null}) is BAD_SHAPE.
     */
    private static JsonNode parsePartition(JsonNode keyNode) throws IOException {
        if (keyNode.isArray()) {
            if (keyNode.size() != 2) {
                throw new IOException("key array must have exactly two elements");
            }
            JsonNode engine = keyNode.get(0);
            if (engine == null || !engine.isTextual() || engine.asText().isEmpty()) {
                throw new IOException("key array element 0 must be a non-empty engine name string");
            }
            JsonNode partition = keyNode.get(1);
            if (partition == null || !partition.isObject()) {
                throw new IOException("key array element 1 must be the partition object");
            }
            JsonNode server = partition.get("server");
            if (server == null || !server.isTextual() || server.asText().isEmpty()) {
                throw new IOException("partition object must contain a non-empty string member \"server\"");
            }
            return partition;
        }
        JsonNode member = unwrapEnvelope(keyNode).get("partition");
        if (member == null || !member.isObject()) {
            throw new IOException("key object does not contain an object member \"partition\"");
        }
        return member;
    }

    /**
     * Engine format: the value is the offset map itself (may be the empty object).
     * Connect format: object with an object {@code offset} member.
     */
    private static JsonNode parseOffset(JsonNode keyNode, JsonNode valueNode) throws IOException {
        JsonNode payload = unwrapEnvelope(valueNode);
        if (keyNode.isArray()) {
            return payload;
        }
        JsonNode member = payload.get("offset");
        if (member == null || !member.isObject()) {
            throw new IOException("value object does not contain an object member \"offset\"");
        }
        return member;
    }

    /** Accepts bare JSON or the schemaful {@code {"schema":{...},"payload":{...}}} envelope. */
    private static JsonNode unwrapEnvelope(JsonNode node) {
        JsonNode schema = node.get("schema");
        JsonNode payload = node.get("payload");
        if (schema != null && payload != null && payload.isObject()) {
            return payload;
        }
        return node;
    }

    private static void validateScalarValues(JsonNode map, String what) throws IOException {
        for (JsonNode value : map) {
            if (!(value.isTextual() || value.isNumber() || value.isBoolean())) {
                throw new IOException(what + " object values must be scalars (string, number or boolean)");
            }
        }
    }

    private static JsonNode readContainerNode(byte[] bytes, String what) throws IOException {
        JsonNode node = readTree(bytes, what);
        if (!node.isObject() && !node.isArray()) {
            throw new IOException(what + " bytes are not a JSON object or array");
        }
        return node;
    }

    private static JsonNode readObjectNode(byte[] bytes, String what) throws IOException {
        JsonNode node = readTree(bytes, what);
        if (!node.isObject()) {
            throw new IOException(what + " bytes are not a JSON object");
        }
        return node;
    }

    private static JsonNode readTree(byte[] bytes, String what) throws IOException {
        JsonNode node;
        try {
            node = MAPPER.readTree(bytes);
        } catch (JsonProcessingException e) {
            throw new IOException(what + " bytes are not valid JSON: " + e.getMessage());
        }
        if (node == null || node.isMissingNode()) {
            throw new IOException(what + " bytes are not valid JSON");
        }
        return node;
    }

    private static String className(Object value) {
        return value == null ? "null" : value.getClass().getName();
    }

    private static void write(PrintStream out, String json) {
        byte[] payload = (json + "\n").getBytes(StandardCharsets.UTF_8);
        out.write(payload, 0, payload.length);
        out.flush();
    }
}
