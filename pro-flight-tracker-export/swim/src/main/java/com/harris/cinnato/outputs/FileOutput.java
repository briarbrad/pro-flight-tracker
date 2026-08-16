/*
 * Copyright (c) 2021 L3Harris Technologies
 */
package com.harris.cinnato.outputs;

import com.typesafe.config.Config;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Outputs the messages to a single rotating file log located in `./log/messages.log`
 */
public class FileOutput extends Output {
    private static final Logger logger = LoggerFactory.getLogger("file");

    /**
     * Calls superclass constructor on Config parameter
     *
     * @param config the consumer config properties
     */
    public FileOutput(Config config) {
        super(config);
    }

    /**
     * Logs message to a file
     *
     * @param message the incoming message.
     */
    @Override
    public void output(String message, String header) {
        if (this.config.getBoolean("headers")) {
            logger.info(header + this.convert(message));
        } else {
            logger.info(this.convert(message));
        }
    }
}
