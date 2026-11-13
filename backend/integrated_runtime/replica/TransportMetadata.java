package io.openontology.replica;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Map;
import org.apache.kafka.common.config.ConfigDef;
import org.apache.kafka.connect.data.Field;
import org.apache.kafka.connect.data.Schema;
import org.apache.kafka.connect.data.SchemaBuilder;
import org.apache.kafka.connect.data.Struct;
import org.apache.kafka.connect.errors.DataException;
import org.apache.kafka.connect.source.SourceRecord;
import org.apache.kafka.connect.transforms.Transformation;

/**
 * Kafka Connect SMT that wraps each {@link SourceRecord} of the replica stream into an
 * {@code io.openontology.replica.TransportRecord} envelope.
 *
 * <p>The returned record is built via {@link SourceRecord#newRecord}, so the original
 * topic, kafkaPartition, timestamp, headers, key (with its original key schema) and,
     * critically, {@code sourcePartition}/{@code sourceOffset} are preserved unchanged.
     * These are connector resume coordinates: they can describe progress within a
     * transaction and are not, by themselves, complete-transaction commit positions.
     * The original payload is embedded unmodified as the {@code event} field so consumers
     * can validate transaction END metadata before advancing an applied cursor. Offset maps contain

 * positions only, never runtime credentials. Partition/offset maps are serialized
 * losslessly to JSON strings with Jackson (no {@code toString()}).
 *
 * <p>Envelope schema: name {@code io.openontology.replica.TransportRecord}, fields in
 * order: {@code topic} STRING, {@code partition_json} STRING, {@code offset_json}
 * STRING, {@code event} (original value schema, optional; null for tombstones),
 * {@code key} (original key schema, optional; field omitted when the record has no key
 * schema).
 */
public class TransportMetadata implements Transformation<SourceRecord> {

    public static final String SCHEMA_NAME = "io.openontology.replica.TransportRecord";

    public static final String FIELD_TOPIC = "topic";
    public static final String FIELD_PARTITION_JSON = "partition_json";
    public static final String FIELD_OFFSET_JSON = "offset_json";
    public static final String FIELD_EVENT = "event";
    public static final String FIELD_KEY = "key";

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Override
    public void configure(Map<String, ?> configs) {
        // no configuration options
    }

    @Override
    public SourceRecord apply(SourceRecord record) {
        if (record == null) {
            return null;
        }
        if (record.value() != null && record.valueSchema() == null) {
            throw new DataException("schemaless record values are not supported by " + SCHEMA_NAME);
        }
        if (record.key() != null && record.keySchema() == null) {
            throw new DataException("schemaless record keys are not supported by " + SCHEMA_NAME);
        }

        SchemaBuilder builder = SchemaBuilder.struct()
                .name(SCHEMA_NAME)
                .field(FIELD_TOPIC, Schema.STRING_SCHEMA)
                .field(FIELD_PARTITION_JSON, Schema.STRING_SCHEMA)
                .field(FIELD_OFFSET_JSON, Schema.STRING_SCHEMA)
                .field(FIELD_EVENT, eventFieldSchema(record));
        Schema keyFieldSchema = keyFieldSchema(record);
        if (keyFieldSchema != null) {
            builder.field(FIELD_KEY, keyFieldSchema);
        }
        Schema envelopeSchema = builder.build();

        Struct envelope = new Struct(envelopeSchema)
                .put(FIELD_TOPIC, record.topic())
                .put(FIELD_PARTITION_JSON, toJson(record.sourcePartition()))
                .put(FIELD_OFFSET_JSON, toJson(record.sourceOffset()))
                .put(FIELD_EVENT, record.value());
        if (record.key() != null || record.keySchema() != null) {
            envelope.put(FIELD_KEY, record.key());
        }
        envelope.validate();

        return record.newRecord(
                record.topic(),
                record.kafkaPartition(),
                record.keySchema(),
                record.key(),
                envelopeSchema,
                envelope,
                record.timestamp(),
                record.headers());
    }

    /**
     * The {@code event} field references the record's original value schema exactly, so
     * the embedded {@link Struct} keeps its own schema instance (Kafka Connect compares
     * schemas on {@code Struct.put}). Only a null-valued event is stored as optional:
     * tombstones (null schema) use an optional placeholder that always carries null,
     * and a null value with a non-null schema gets an optional copy of that schema.
     */
    private static Schema eventFieldSchema(SourceRecord record) {
        Schema valueSchema = record.valueSchema();
        if (valueSchema == null) {
            return Schema.OPTIONAL_STRING_SCHEMA;
        }
        return record.value() == null ? optionalCopy(valueSchema) : valueSchema;
    }

    /** Same rule as {@link #eventFieldSchema(SourceRecord)} for the {@code key} field. */
    private static Schema keyFieldSchema(SourceRecord record) {
        if (record.keySchema() == null) {
            return null;
        }
        return record.key() == null ? optionalCopy(record.keySchema()) : record.keySchema();
    }

    /**
     * Rebuilds the schema as optional, preserving type, name, version, doc, parameters,
     * default value and (for structs) all fields; nested field schemas are referenced
     * as-is so nothing is lost. Returns the input unchanged when already optional.
     * Only used for null-valued fields, where Connect skips schema equality checks.
     */
    private static Schema optionalCopy(Schema source) {
        if (source == null || source.isOptional()) {
            return source;
        }
        SchemaBuilder builder;
        switch (source.type()) {
            case INT8:
                builder = SchemaBuilder.int8();
                break;
            case INT16:
                builder = SchemaBuilder.int16();
                break;
            case INT32:
                builder = SchemaBuilder.int32();
                break;
            case INT64:
                builder = SchemaBuilder.int64();
                break;
            case FLOAT32:
                builder = SchemaBuilder.float32();
                break;
            case FLOAT64:
                builder = SchemaBuilder.float64();
                break;
            case BOOLEAN:
                builder = SchemaBuilder.bool();
                break;
            case STRING:
                builder = SchemaBuilder.string();
                break;
            case BYTES:
                builder = SchemaBuilder.bytes();
                break;
            case ARRAY:
                builder = SchemaBuilder.array(source.valueSchema());
                break;
            case MAP:
                builder = SchemaBuilder.map(source.keySchema(), source.valueSchema());
                break;
            case STRUCT:
                builder = SchemaBuilder.struct();
                if (source.name() != null) {
                    builder.name(source.name());
                }
                if (source.version() != null) {
                    builder.version(source.version());
                }
                for (Field field : source.fields()) {
                    builder.field(field.name(), field.schema());
                }
                break;
            default:
                return source;
        }
        if (source.doc() != null) {
            builder.doc(source.doc());
        }
        if (source.parameters() != null && !source.parameters().isEmpty()) {
            for (Map.Entry<String, String> param : source.parameters().entrySet()) {
                builder.parameter(param.getKey(), param.getValue());
            }
        }
        if (source.defaultValue() != null) {
            builder.defaultValue(source.defaultValue());
        }
        return builder.optional().build();
    }

    private static String toJson(Map<String, ?> map) {
        if (map == null) {
            return "{}";
        }
        try {
            return MAPPER.writeValueAsString(map);
        } catch (JsonProcessingException e) {
            throw new DataException("failed to serialize source partition/offset map to JSON", e);
        }
    }

    @Override
    public ConfigDef config() {
        return new ConfigDef();
    }

    @Override
    public void close() {
        // nothing to release
    }
}
