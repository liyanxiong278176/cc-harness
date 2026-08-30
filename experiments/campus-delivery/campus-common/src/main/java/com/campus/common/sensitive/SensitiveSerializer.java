package com.campus.common.sensitive;

import com.fasterxml.jackson.core.JsonGenerator;
import com.fasterxml.jackson.databind.JsonSerializer;
import com.fasterxml.jackson.databind.SerializerProvider;
import com.fasterxml.jackson.databind.ser.ContextualSerializer;

import java.io.IOException;

/**
 * 脱敏序列化器: 读取字段上的 {@link Sensitive} 注解类型,出参时脱敏。
 */
public class SensitiveSerializer extends JsonSerializer<String> implements ContextualSerializer {

    private final SensitiveType type;

    public SensitiveSerializer() {
        this(SensitiveType.NONE);
    }

    public SensitiveSerializer(SensitiveType type) {
        this.type = type;
    }

    @Override
    public void serialize(String value, JsonGenerator gen, SerializerProvider serializers) throws IOException {
        gen.writeString(type.mask(value));
    }

    @Override
    public JsonSerializer<?> createContextual(SerializerProvider prov,
                                              com.fasterxml.jackson.databind.BeanProperty property) {
        if (property != null) {
            Sensitive ann = property.getAnnotation(Sensitive.class);
            if (ann != null) {
                return new SensitiveSerializer(ann.value());
            }
        }
        return this;
    }
}
