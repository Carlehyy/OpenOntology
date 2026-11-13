package io.openontology.replica;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.ObjectOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.apache.kafka.connect.data.Schema;
import org.apache.kafka.connect.data.SchemaBuilder;
import org.apache.kafka.connect.data.Struct;
import org.apache.kafka.connect.errors.DataException;
import org.apache.kafka.connect.header.ConnectHeaders;
import org.apache.kafka.connect.header.Headers;
import org.apache.kafka.connect.source.SourceRecord;

/**
 * Standalone self-test for {@link TransportMetadata} and {@link ReadOffsets}.
 *
 * <p>Run inside the pinned Debezium image (no network, 512m):
 * <pre>
 * java -cp "&lt;out&gt;/classes:&lt;out&gt;/test-classes:/debezium/lib/*" io.openontology.replica.SelfTest
 * </pre>
 */
public final class SelfTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static int checks = 0;

    public static void main(String[] args) throws Exception {
        TransportMetadata smt = new TransportMetadata();
        smt.configure(Map.of());

        testDataRecord(smt);
        testTombstone(smt);
        testHeartbeat(smt);
        testTransactionMetadata(smt);
        testSchemalessRejected(smt);

        byte[] validFile = testReadOffsetsValid();
        testReadOffsetsEngineFormValid();
        testReadOffsetsEmptyStates();
        testReadOffsetsRejectsForeignObjects();
        testReadOffsetsTruncated(validFile);
        testReadOffsetsTrailingData(validFile);
        testReadOffsetsBadShape();
        testReadOffsetsIllegalArrays();
        testReadOffsetsNullEntries();
        testReadOffsetsOversize();

        smt.close();
        System.out.println("SELFTEST PASSED, checks=" + checks);
    }

    // ------------------------------------------------------------------ SMT tests

    private static void testDataRecord(TransportMetadata smt) throws IOException {
        Schema rowSchema = SchemaBuilder.struct().name("svr.public.orders.Value")
                .field("id", Schema.INT64_SCHEMA)
                .field("amount", Schema.FLOAT64_SCHEMA)
                .field("note", Schema.OPTIONAL_STRING_SCHEMA)
                .optional()
                .build();
        Schema keySchema = SchemaBuilder.struct().name("svr.public.orders.Key")
                .field("id", Schema.INT64_SCHEMA)
                .build();
        Schema sourceSchema = SchemaBuilder.struct().name("io.debezium.connector.postgresql.Source")
                .field("lsn", Schema.OPTIONAL_INT64_SCHEMA) // row LSN, intentionally != commit LSN
                .field("snapshot", Schema.OPTIONAL_STRING_SCHEMA)
                .build();
        Schema envelopeIn = SchemaBuilder.struct().name("svr.public.orders.Envelope")
                .field("before", rowSchema)
                .field("after", rowSchema)
                .field("source", sourceSchema)
                .field("op", Schema.STRING_SCHEMA)
                .field("ts_ms", Schema.INT64_SCHEMA)
                .build();

        Struct key = new Struct(keySchema).put("id", 1L);
        Struct after = new Struct(rowSchema).put("id", 1L).put("amount", 12.5);
        Struct source = new Struct(sourceSchema).put("lsn", 100L); // row LSN 100
        Struct value = new Struct(envelopeIn)
                .put("before", null)
                .put("after", after)
                .put("source", source)
                .put("op", "c")
                .put("ts_ms", 1731398400123L);

        Map<String, Object> sourcePartition = new LinkedHashMap<>();
        sourcePartition.put("server", "svr");
        Map<String, Object> sourceOffset = new LinkedHashMap<>();
        sourceOffset.put("lsn", 5550000001234567L); // commit LSN
        sourceOffset.put("last_processed_lsn", 5550000001234566L);
        sourceOffset.put("txId", 4242L);
        sourceOffset.put("ts_ns", 1731398400123456789L);

        Headers headers = new ConnectHeaders()
                .add("h1", "v1", Schema.STRING_SCHEMA)
                .add("h2", new byte[]{1, 2, 3}, Schema.BYTES_SCHEMA);
        SourceRecord record = new SourceRecord(
                sourcePartition, sourceOffset, "svr.public.orders", 3,
                keySchema, key, envelopeIn, value, 1731398400123L, headers);

        SourceRecord out = smt.apply(record);

        check(out != record, "data: new record instance");
        check("svr.public.orders".equals(out.topic()), "data: topic preserved");
        check(Integer.valueOf(3).equals(out.kafkaPartition()), "data: kafkaPartition preserved");
        check(Long.valueOf(1731398400123L).equals(out.timestamp()), "data: timestamp preserved");
        check(sourcePartition.equals(out.sourcePartition()), "data: sourcePartition preserved");
        check(sourceOffset.equals(out.sourceOffset()), "data: sourceOffset preserved");
        check(out.keySchema().equals(keySchema), "data: key schema preserved");
        check(out.key() instanceof Struct && out.key().equals(key), "data: key struct preserved");
        check(out.headers().size() == 2, "data: header count preserved");
        check("v1".equals(out.headers().lastWithName("h1").value()), "data: header h1 preserved");
        check(Arrays.equals(new byte[]{1, 2, 3}, (byte[]) out.headers().lastWithName("h2").value()),
                "data: header h2 preserved");
        check(record.value() == value && record.valueSchema() == envelopeIn,
                "data: input record not mutated");

        Schema vs = out.valueSchema();
        check(TransportMetadata.SCHEMA_NAME.equals(vs.name()), "data: envelope schema name");
        check(vs.type() == Schema.Type.STRUCT, "data: envelope is struct");
        check(vs.fields().size() == 5, "data: five envelope fields");
        check(vs.fields().get(0).name().equals("topic")
                        && vs.fields().get(1).name().equals("partition_json")
                        && vs.fields().get(2).name().equals("offset_json")
                        && vs.fields().get(3).name().equals("event")
                        && vs.fields().get(4).name().equals("key"),
                "data: envelope field order");
        check(vs.field("topic").schema().type() == Schema.Type.STRING, "data: topic STRING");
        check(vs.field("partition_json").schema().type() == Schema.Type.STRING, "data: partition_json STRING");
        check(vs.field("offset_json").schema().type() == Schema.Type.STRING, "data: offset_json STRING");
        check(vs.field("event").schema().equals(envelopeIn), "data: event uses original value schema");
        check("svr.public.orders.Envelope".equals(vs.field("event").schema().name()),
                "data: event keeps original schema name");
        check(vs.field("key").schema().equals(keySchema), "data: key uses original key schema");

        Struct envelope = (Struct) out.value();
        envelope.validate();
        check("svr.public.orders".equals(envelope.get("topic")), "data: topic value");
        check(jsonEqual(MAPPER.valueToTree(sourcePartition), MAPPER.readTree(envelope.getString("partition_json"))),
                "data: partition_json lossless");
        check(jsonEqual(MAPPER.valueToTree(sourceOffset), MAPPER.readTree(envelope.getString("offset_json"))),
                "data: offset_json lossless");
        JsonNode offsetJson = MAPPER.readTree(envelope.getString("offset_json"));
        check(offsetJson.get("lsn").longValue() == 5550000001234567L,
                "data: offset lsn is commit LSN, not payload row LSN");
        check(offsetJson.get("ts_ns").longValue() == 1731398400123456789L, "data: ts_ns exact");
        check(!offsetJson.has("op") && !offsetJson.has("source"), "data: offset not sourced from payload");
        check(value.equals(envelope.get("event")), "data: original event struct embedded");
        check(key.equals(envelope.get("key")), "data: original key struct embedded");
    }

    private static void testTombstone(TransportMetadata smt) {
        Schema keySchema = SchemaBuilder.struct().name("svr.public.orders.Key")
                .field("id", Schema.INT64_SCHEMA).build();
        Struct key = new Struct(keySchema).put("id", 7L);
        Map<String, Object> offset = Map.of("lsn", 42L);

        SourceRecord tombstone = new SourceRecord(
                Map.of("server", "svr"), offset, "svr.public.orders", 0,
                keySchema, key, null, null, null);

        SourceRecord out = smt.apply(tombstone);
        Struct envelope = (Struct) out.value();
        envelope.validate();
        check(out.sourceOffset().equals(offset), "tombstone: offset preserved");
        check(out.valueSchema().field("event") != null && out.valueSchema().field("event").schema().isOptional(),
                "tombstone: event field present and optional");
        check(envelope.get("event") == null, "tombstone: event null");
        check(key.equals(envelope.get("key")) && key.equals(out.key()), "tombstone: key preserved");
        check(keySchema.equals(out.keySchema()), "tombstone: key schema preserved");
        JsonNode tombOffsetJson;
        try {
            tombOffsetJson = MAPPER.readTree(envelope.getString("offset_json"));
        } catch (IOException e) {
            throw new AssertionError("tombstone: offset_json not JSON", e);
        }
        check(tombOffsetJson.get("lsn").longValue() == 42L, "tombstone: offset_json lossless");
    }

    private static void testHeartbeat(TransportMetadata smt) {
        Schema hbSchema = SchemaBuilder.struct().name("io.debezium.connector.common.Heartbeat")
                .field("ts_ns", Schema.INT64_SCHEMA).build();
        Struct hb = new Struct(hbSchema).put("ts_ns", 1731398400999999999L);
        Map<String, Object> offset = Map.of("lsn", 43L, "xmin", 5L);

        SourceRecord record = new SourceRecord(
                Map.of("server", "svr"), offset, "heartbeat-svr", null,
                null, null, hbSchema, hb, null);

        SourceRecord out = smt.apply(record);
        Struct envelope = (Struct) out.value();
        envelope.validate();
        check(out.valueSchema().field("key") == null, "heartbeat: no key field when key schema absent");
        check(hb.equals(envelope.get("event")), "heartbeat: event preserved");
        check(out.sourceOffset().equals(offset), "heartbeat: offset preserved");
        check(out.sourcePartition().equals(Map.of("server", "svr")), "heartbeat: partition preserved");
        JsonNode offsetJson;
        try {
            offsetJson = MAPPER.readTree(envelope.getString("offset_json"));
        } catch (IOException e) {
            throw new AssertionError("heartbeat: offset_json not JSON", e);
        }
        check(offsetJson.get("lsn").longValue() == 43L && offsetJson.get("xmin").longValue() == 5L,
                "heartbeat: offset_json lossless");
    }

    private static void testTransactionMetadata(TransportMetadata smt) {
        Schema txKeySchema = SchemaBuilder.struct().name("svr.transaction.Key")
                .field("transaction_id", Schema.STRING_SCHEMA).build();
        Schema dataCollectionSchema = SchemaBuilder.struct().name("svr.transaction.DataCollection")
                .field("data_collection", Schema.STRING_SCHEMA)
                .field("event_count", Schema.OPTIONAL_INT32_SCHEMA)
                .build();
        Schema txValueSchema = SchemaBuilder.struct().name("svr.transaction.Envelope")
                .field("id", Schema.STRING_SCHEMA)
                .field("status", Schema.STRING_SCHEMA)
                .field("event_count", Schema.OPTIONAL_INT32_SCHEMA)
                .field("data_collections", SchemaBuilder.array(dataCollectionSchema).optional().build())
                .build();

        Struct txKey = new Struct(txKeySchema).put("transaction_id", "pid:1:ts:1");
        Struct dc = new Struct(dataCollectionSchema)
                .put("data_collection", "svr.public.orders")
                .put("event_count", 7);
        Struct txValue = new Struct(txValueSchema)
                .put("id", "pid:1:ts:1")
                .put("status", "END")
                .put("event_count", 7)
                .put("data_collections", List.of(dc));
        Map<String, Object> offset = Map.of("lsn", 44L);

        SourceRecord record = new SourceRecord(
                Map.of("server", "svr"), offset, "svr.transaction", null,
                txKeySchema, txKey, txValueSchema, txValue, null);

        SourceRecord out = smt.apply(record);
        Struct envelope = (Struct) out.value();
        envelope.validate();
        check(txValue.equals(envelope.get("event")), "transaction: event struct preserved");
        check(txKey.equals(envelope.get("key")), "transaction: key struct preserved");
        check(txKeySchema.equals(out.keySchema()) && txKey.equals(out.key()),
                "transaction: record-level key preserved");
        check(out.sourceOffset().equals(offset), "transaction: offset preserved");
        check(out.valueSchema().field("event").schema().field("data_collections") != null,
                "transaction: nested array field survives");
    }

    private static void testSchemalessRejected(TransportMetadata smt) {
        SourceRecord schemaless = new SourceRecord(
                Map.of("server", "svr"), Map.of("lsn", 1L), "t", null,
                null, null, null, Map.of("k", "v"), null);
        boolean threw = false;
        try {
            smt.apply(schemaless);
        } catch (DataException expected) {
            threw = true;
        }
        check(threw, "schemaless value rejected with DataException");
    }

    // ----------------------------------------------------------- ReadOffsets tests

    private static byte[] serialize(Object object) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        try (ObjectOutputStream oos = new ObjectOutputStream(bos)) {
            oos.writeObject(object);
        }
        return bos.toByteArray();
    }

    private static Path tempFile(byte[] bytes) throws IOException {
        Path path = Files.createTempFile("replica-offsets", ".dat");
        Files.write(path, bytes);
        return path;
    }

    private static final class RunResult {
        final int code;
        final String out;
        final String err;

        RunResult(int code, String out, String err) {
            this.code = code;
            this.out = out;
            this.err = err;
        }
    }

    private static RunResult runRead(Path file) {
        ByteArrayOutputStream outBuf = new ByteArrayOutputStream();
        ByteArrayOutputStream errBuf = new ByteArrayOutputStream();
        int code;
        try (PrintStream out = new PrintStream(outBuf, true, StandardCharsets.UTF_8);
             PrintStream err = new PrintStream(errBuf, true, StandardCharsets.UTF_8)) {
            code = ReadOffsets.run(file, out, err);
        }
        return new RunResult(code, outBuf.toString(StandardCharsets.UTF_8), errBuf.toString(StandardCharsets.UTF_8));
    }

    private static byte[] bytes(String text) {
        return text.getBytes(StandardCharsets.UTF_8);
    }

    /** Builds a file in the exact FileOffsetBackingStore format and returns its bytes. */
    private static byte[] testReadOffsetsValid() throws IOException {
        Map<byte[], byte[]> raw = new HashMap<>();
        raw.put(bytes("{\"connector\":\"svr\",\"partition\":{\"server\":\"db1\"}}"),
                bytes("{\"offset\":{\"lsn\":5550000001234567,\"last_processed_lsn\":5550000001234566,"
                        + "\"txId\":4242,\"ts_ns\":1731398400123456789}}"));
        raw.put(bytes("{\"connector\":\"svr\",\"partition\":{\"server\":\"db2\"}}"),
                bytes("{\"offset\":{\"lsn\":42}}"));
        raw.put(bytes("{\"schema\":{\"type\":\"struct\"},\"payload\":{\"connector\":\"svr\",\"partition\":{\"server\":\"db3\"}}}"),
                bytes("{\"schema\":{\"type\":\"struct\"},\"payload\":{\"offset\":{\"lsn\":7}}}"));
        byte[] file = serialize(raw);
        RunResult r = runRead(tempFile(file));
        check(r.code == 0, "readOffsets: valid file exit 0, got " + r.code + " err=" + r.err);
        String json = r.out.strip();
        check(json.indexOf('\n') < 0, "readOffsets: single-line output");
        JsonNode array = MAPPER.readTree(json);
        check(array.isArray() && array.size() == 3, "readOffsets: three entries");
        JsonNode first = array.get(0);
        check("db1".equals(first.get("partition").get("server").asText()), "readOffsets: sorted first is db1");
        check(first.get("offset").get("lsn").longValue() == 5550000001234567L, "readOffsets: commit lsn lossless");
        check(first.get("offset").get("ts_ns").longValue() == 1731398400123456789L, "readOffsets: ts_ns lossless");
        check(first.get("offset").get("txId").longValue() == 4242L, "readOffsets: txId preserved");
        List<String> names = new ArrayList<>();
        first.fieldNames().forEachRemaining(names::add);
        check(names.equals(List.of("partition", "offset")), "readOffsets: entry has exactly partition and offset");
        check("db2".equals(array.get(1).get("partition").get("server").asText())
                && array.get(1).get("offset").get("lsn").longValue() == 42L, "readOffsets: db2 entry");
        check("db3".equals(array.get(2).get("partition").get("server").asText())
                && array.get(2).get("offset").get("lsn").longValue() == 7L,
                "readOffsets: schemaful {schema,payload} envelope unwrapped");
        return file;
    }

    /** Debezium Server 3.7.1.Final engine format: key ["<engine>",{"server":...}], value = offset map itself. */
    private static void testReadOffsetsEngineFormValid() throws IOException {
        Map<byte[], byte[]> raw = new HashMap<>();
        raw.put(bytes("[\"http\",{\"server\":\"replicainitiala\"}]"),
                bytes("{\"lsn_proc\":115750584,\"messageType\":\"COMMIT\",\"lsn_commit\":115750584,"
                        + "\"lsn\":115750584,\"txId\":906,\"ts_usec\":1794423142581579}"));
        raw.put(bytes("[\"http\",{\"server\":\"replicainitialb\"}]"), bytes("{}"));
        byte[] file = serialize(raw);
        RunResult r = runRead(tempFile(file));
        check(r.code == 0, "engine form: exit 0, got " + r.code + " err=" + r.err);
        String json = r.out.strip();
        check(json.indexOf('\n') < 0, "engine form: single-line output");
        JsonNode array = MAPPER.readTree(json);
        check(array.isArray() && array.size() == 2, "engine form: two entries");
        JsonNode first = array.get(0);
        check("replicainitiala".equals(first.get("partition").get("server").asText()),
                "engine form: first partition is replicainitiala");
        check(first.get("offset").get("lsn").longValue() == 115750584L, "engine form: lsn exact");
        check(first.get("offset").get("ts_usec").longValue() == 1794423142581579L,
                "engine form: ts_usec exact");
        check("COMMIT".equals(first.get("offset").get("messageType").asText()),
                "engine form: string scalar preserved");
        List<String> names = new ArrayList<>();
        first.fieldNames().forEachRemaining(names::add);
        check(names.equals(List.of("partition", "offset")),
                "engine form: entry has exactly partition and offset");
        JsonNode second = array.get(1);
        check("replicainitialb".equals(second.get("partition").get("server").asText()),
                "engine form: empty-offset partition kept");
        check(second.get("offset").isObject() && second.get("offset").size() == 0,
                "engine form: empty offset object reported, not dropped");
    }

    private static void testReadOffsetsIllegalArrays() throws IOException {
        String[][] cases = {
            {"[\"http\",{\"server\":\"a\"},\"extra\"]", "{\"lsn\":1}"},
            {"[\"http\"]", "{\"lsn\":1}"},
            {"[123,{\"server\":\"a\"}]", "{\"lsn\":1}"},
            {"[\"\",{\"server\":\"a\"}]", "{\"lsn\":1}"},
            {"[\"http\",\"notobject\"]", "{\"lsn\":1}"},
            {"[\"http\",{}]", "{\"lsn\":1}"},
            {"[\"http\",{\"server\":\"\"}]", "{\"lsn\":1}"},
            {"[\"http\",{\"server\":123}]", "{\"lsn\":1}"},
            {"[\"http\",{\"server\":\"a\",\"nested\":{\"x\":1}}]", "{\"lsn\":1}"},
            {"[\"http\",{\"server\":\"a\",\"list\":[1]}]", "{\"lsn\":1}"},
            {"[\"http\",{\"server\":\"a\",\"nul\":null}]", "{\"lsn\":1}"},
            {"[\"http\",{\"server\":\"a\"}]", "{\"offset\":{\"nested\":{}}}"},
            {"[null,{\"server\":\"a\"}]", "{\"lsn\":1}"},
            {"[\"http\",null]", "{\"lsn\":1}"},
            {"null", "{\"lsn\":1}"},
            {"[\"http\",{\"server\":\"a\"}]", "null"},
            {"\"http\"", "{\"lsn\":1}"},
        };
        for (String[] probe : cases) {
            Map<byte[], byte[]> single = new HashMap<>();
            single.put(bytes(probe[0]), bytes(probe[1]));
            RunResult r = runRead(tempFile(serialize(single)));
            check(r.code == 5, "illegal shape " + probe[0] + " / " + probe[1]
                    + " -> exit 5, got " + r.code + " " + r.err);
        }
    }

    private static void testReadOffsetsNullEntries() throws IOException {
        Map<byte[], byte[]> nullValue = new HashMap<>();
        nullValue.put(bytes("[\"http\",{\"server\":\"a\"}]"), null);
        RunResult r1 = runRead(tempFile(serialize(nullValue)));
        check(r1.code == 3, "null map value rejected exit 3, got " + r1.code + " " + r1.err);

        Map<byte[], byte[]> nullKey = new HashMap<>();
        nullKey.put(null, bytes("{\"lsn\":1}"));
        RunResult r2 = runRead(tempFile(serialize(nullKey)));
        check(r2.code == 3, "null map key rejected exit 3, got " + r2.code);
    }

    private static void testReadOffsetsOversize() throws IOException {
        Path big = Files.createTempFile("replica-oversize", ".dat");
        try {
            try (java.io.OutputStream os = Files.newOutputStream(big)) {
                byte[] chunk = new byte[1024 * 1024];
                long written = 0;
                while (written <= ReadOffsets.MAX_BYTES) {
                    os.write(chunk);
                    written += chunk.length;
                }
            }
            RunResult r = runRead(big);
            check(r.code == 3, "oversize file rejected exit 3, got " + r.code);
            check(r.out.strip().isEmpty(), "oversize file produces no stdout");
        } finally {
            Files.deleteIfExists(big);
        }
    }

    private static void testReadOffsetsEmptyStates() throws IOException {
        RunResult missing = runRead(Path.of("/tmp/replica-selftest-missing-offsets.dat"));
        check(missing.code == 0 && "[]".equals(missing.out.strip()),
                "readOffsets: missing file -> [] exit 0");
        RunResult empty = runRead(tempFile(new byte[0]));
        check(empty.code == 0 && "[]".equals(empty.out.strip()),
                "readOffsets: empty file -> [] exit 0");
    }

    private static void testReadOffsetsRejectsForeignObjects() throws IOException {
        Map<Object, Object> stringKey = new HashMap<>();
        stringKey.put("connector", new byte[]{1});
        RunResult r1 = runRead(tempFile(serialize(stringKey)));
        check(r1.code == 3, "readOffsets: String key rejected exit 3, got " + r1.code + " " + r1.err);
        check(r1.out.strip().isEmpty(), "readOffsets: no stdout on rejection");

        Map<Object, Object> gadget = new HashMap<>();
        gadget.put(new Date(0), new byte[]{1}); // any non-allowlisted class must die before readObject
        RunResult r2 = runRead(tempFile(serialize(gadget)));
        check(r2.code == 3, "readOffsets: foreign object key rejected exit 3, got " + r2.code);

        RunResult r3 = runRead(tempFile(serialize(List.of("a"))));
        check(r3.code == 3, "readOffsets: non-HashMap top level rejected exit 3, got " + r3.code);

        RunResult r4 = runRead(tempFile(serialize("plain string")));
        check(r4.code == 3, "readOffsets: String top level rejected exit 3, got " + r4.code);
    }

    private static void testReadOffsetsTruncated(byte[] validFile) throws IOException {
        byte[] cut = Arrays.copyOf(validFile, validFile.length * 2 / 3);
        RunResult r = runRead(tempFile(cut));
        check(r.code == 4, "readOffsets: truncated mid-entry rejected exit 4, got " + r.code + " " + r.err);
        check(r.out.strip().isEmpty(), "readOffsets: no partial stdout on truncation");

        RunResult headerCut = runRead(tempFile(Arrays.copyOf(validFile, 10)));
        check(headerCut.code == 4, "readOffsets: stream-header truncation exit 4, got " + headerCut.code);
    }

    private static void testReadOffsetsTrailingData(byte[] validFile) throws IOException {
        byte[] tampered = Arrays.copyOf(validFile, validFile.length + 1);
        tampered[validFile.length] = 0x42;
        RunResult r = runRead(tempFile(tampered));
        check(r.code == 4, "readOffsets: trailing data rejected exit 4, got " + r.code);
    }

    private static void testReadOffsetsBadShape() throws IOException {
        Map<byte[], byte[]> notJson = new HashMap<>();
        notJson.put(bytes("{\"connector\":\"svr\",\"partition\":{\"server\":\"x\"}}"), bytes("not json at all"));
        RunResult r1 = runRead(tempFile(serialize(notJson)));
        check(r1.code == 5, "readOffsets: non-JSON value exit 5, got " + r1.code + " " + r1.err);

        Map<byte[], byte[]> noOffset = new HashMap<>();
        noOffset.put(bytes("{\"connector\":\"svr\",\"partition\":{\"server\":\"x\"}}"), bytes("{\"nope\":1}"));
        RunResult r2 = runRead(tempFile(serialize(noOffset)));
        check(r2.code == 5, "readOffsets: value without offset member exit 5, got " + r2.code);

        Map<byte[], byte[]> offsetNotObject = new HashMap<>();
        offsetNotObject.put(bytes("{\"connector\":\"svr\",\"partition\":{\"server\":\"x\"}}"),
                bytes("{\"offset\":\"str\"}"));
        RunResult r3 = runRead(tempFile(serialize(offsetNotObject)));
        check(r3.code == 5, "readOffsets: non-object offset exit 5, got " + r3.code);

        Map<byte[], byte[]> noPartition = new HashMap<>();
        noPartition.put(bytes("{\"connector\":\"svr\"}"), bytes("{\"offset\":{\"lsn\":1}}"));
        RunResult r4 = runRead(tempFile(serialize(noPartition)));
        check(r4.code == 5, "readOffsets: key without partition member exit 5, got " + r4.code);

        Map<byte[], byte[]> partitionNotObject = new HashMap<>();
        partitionNotObject.put(bytes("{\"connector\":\"svr\",\"partition\":\"db1\"}"), bytes("{\"offset\":{}}"));
        RunResult r5 = runRead(tempFile(serialize(partitionNotObject)));
        check(r5.code == 5, "readOffsets: non-object partition exit 5, got " + r5.code);
    }

    /** Compares canonical JSON text so LongNode(4242) equals IntNode(4242). */
    private static boolean jsonEqual(JsonNode left, JsonNode right) throws IOException {
        return MAPPER.writeValueAsString(left).equals(MAPPER.writeValueAsString(right));
    }

    private static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError("FAILED: " + message);
        }
        checks++;
        System.out.println("ok - " + message);
    }
}
