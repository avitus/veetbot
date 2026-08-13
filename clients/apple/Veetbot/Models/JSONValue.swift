import Foundation

public enum JSONValue: Codable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    public var stringValue: String? {
        guard case .string(let value) = self else { return nil }
        return value
    }

    public var intValue: Int? {
        guard case .number(let value) = self, value.rounded() == value else { return nil }
        return Int(exactly: value)
    }

    public var boolValue: Bool? {
        guard case .bool(let value) = self else { return nil }
        return value
    }

    public var objectValue: [String: JSONValue]? {
        guard case .object(let value) = self else { return nil }
        return value
    }

    public var arrayValue: [JSONValue]? {
        guard case .array(let value) = self else { return nil }
        return value
    }

    public var prettyPrinted: String {
        guard
            let data = try? JSONEncoder.pretty.encode(self),
            let string = String(data: data, encoding: .utf8)
        else {
            return ""
        }
        return string
    }
}

extension JSONEncoder {
    static var server: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(ServerDates.string(from: date))
        }
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return encoder
    }

    static var pretty: JSONEncoder {
        let encoder = server
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return encoder
    }
}

extension JSONDecoder {
    static var server: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            guard let date = ServerDates.date(from: value) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Invalid RFC 3339 date: \(value)"
                )
            }
            return date
        }
        return decoder
    }
}

private enum ServerDates {
    private static func formatter(fractionalSeconds: Bool) -> ISO8601DateFormatter {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions =
            fractionalSeconds
            ? [.withInternetDateTime, .withFractionalSeconds]
            : [.withInternetDateTime]
        return formatter
    }

    static func date(from value: String) -> Date? {
        formatter(fractionalSeconds: true).date(from: value)
            ?? formatter(fractionalSeconds: false).date(from: value)
    }

    static func string(from date: Date) -> String {
        formatter(fractionalSeconds: true).string(from: date)
    }
}
