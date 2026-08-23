#!/usr/bin/env python3
"""
Configuration File Generator

This script reads environment variables and generates configuration files
based on JSON configuration templates for different environments (dev/prod).
"""

import os
import json
import argparse
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Union


class ConfigGenerator:
    def __init__(self, config_file: str, env_file: str = "secrets.env"):
        """Initialize the config generator with a JSON config file and optional env file."""
        self.config_file = config_file
        self.env_file = env_file
        self.config = self._load_config()
        self.env_vars = self._load_env_vars()
    
    def _load_env_vars(self) -> Dict[str, str]:
        """Load environment variables from file."""
        env_vars = {}
        
        if not os.path.exists(self.env_file):
            print(f"Warning: Environment file {self.env_file} not found, using system environment")
            return dict(os.environ)
        
        try:
            with open(self.env_file, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    
                    # Skip empty lines and comments
                    if not line or line.startswith('#'):
                        continue
                    
                    # Handle lines with = (KEY=VALUE format)
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        # Remove quotes if present
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        
                        env_vars[key] = value
                    else:
                        print(f"Warning: Skipping malformed line {line_num} in {self.env_file}: {line}")
            
            print(f"Loaded {len(env_vars)} environment variables from {self.env_file}")
            return env_vars
            
        except Exception as e:
            raise ValueError(f"Error reading environment file {self.env_file}: {e}")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file."""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Configuration file {self.config_file} not found")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {self.config_file}: {e}")
    
    def _create_directory(self, filepath: str) -> None:
        """Create directory for the file if it doesn't exist."""
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    
    def _substitute_variables(self, content: str) -> str:
        """Replace ${VAR_NAME} placeholders with environment variable values."""
        for key, value in self.env_vars.items():
            content = content.replace(f"${{{key}}}", str(value))
        return content
    
    def _create_env_file(self, filepath: str, variables: Union[List[str], Dict[str, Union[str, Dict]]]) -> None:
        """Create an environment file (.env)."""
        self._create_directory(filepath)
        
        with open(filepath, 'w') as f:
            # Handle both list format (old) and dict format (new with mapping)
            if isinstance(variables, list):
                # Old format: just variable names
                for var in variables:
                    if var in self.env_vars:
                        f.write(f"{var}={self.env_vars[var]}\n")
                    else:
                        print(f"Warning: Environment variable {var} not found")
            elif isinstance(variables, dict):
                # New format: output_var_name -> source_var_name or transformation
                for output_var, source_config in variables.items():
                    if isinstance(source_config, str):
                        # Simple mapping: "OUTPUT_VAR": "source_var"
                        if source_config in self.env_vars:
                            f.write(f"{output_var}={self.env_vars[source_config]}\n")
                        else:
                            print(f"Warning: Source environment variable {source_config} not found")
                    elif isinstance(source_config, dict):
                        # Complex transformation: "OUTPUT_VAR": {"template": "https://${source_var}", "vars": ["source_var"]}
                        template = source_config.get("template", "")
                        required_vars = source_config.get("vars", [])
                        
                        # Check if all required vars are present
                        missing_vars = [var for var in required_vars if var not in self.env_vars]
                        if missing_vars:
                            print(f"Warning: Missing variables for {output_var}: {missing_vars}")
                            continue
                        
                        # Substitute variables in template
                        value = self._substitute_variables(template)
                        f.write(f"{output_var}={value}\n")
            else:
                print(f"Warning: Invalid variables format for {filepath}")
    
    def _create_properties_file(self, filepath: str, properties: Dict[str, str]) -> None:
        """Create a properties file (.properties)."""
        self._create_directory(filepath)
        
        with open(filepath, 'w') as f:
            for key, value in properties.items():
                # Substitute environment variables in the value
                substituted_value = self._substitute_variables(value)
                f.write(f"{key}={substituted_value}\n")
    
    def _create_htpasswd_file(self, filepath: str, username_var: str, password_var: str) -> None:
        """Create an htpasswd file (.db or .htpasswd)."""
        self._create_directory(filepath)
        
        username = self.env_vars.get(username_var)
        password = self.env_vars.get(password_var)
        
        if not username or not password:
            raise ValueError(f"Username ({username_var}) or password ({password_var}) not found in environment")
        
        try:
            # Run htpasswd command
            subprocess.run([
                'htpasswd', '-c', '-b', '-B', '-C', '10', 
                filepath, username, password
            ], check=True, capture_output=True)
            print(f"Created htpasswd file: {filepath}")
        except subprocess.CalledProcessError as e:
            print(f"Error creating htpasswd file {filepath}: {e}")
        except FileNotFoundError:
            print("Error: htpasswd command not found. Install apache2-utils (Ubuntu) or httpd-tools (RHEL)")
    
    def _create_custom_file(self, filepath: str, content: str) -> None:
        """Create a custom file with specified content."""
        self._create_directory(filepath)
        
        # Substitute environment variables in content
        substituted_content = self._substitute_variables(content)
        
        with open(filepath, 'w') as f:
            f.write(substituted_content)
    
    def generate_files(self) -> None:
        """Generate all configuration files based on the JSON config."""
        files_config = self.config.get('files', {})
        
        for file_info in files_config:
            file_type = file_info.get('type')
            filepath = file_info.get('path')
            
            print(f"Creating {file_type} file: {filepath}")
            
            try:
                if file_type == 'env':
                    variables = file_info.get('variables', [])
                    self._create_env_file(filepath, variables)
                
                elif file_type == 'properties':
                    properties = file_info.get('properties', {})
                    self._create_properties_file(filepath, properties)
                
                elif file_type == 'db':
                    username_var = file_info.get('username_var')
                    password_var = file_info.get('password_var')
                    self._create_htpasswd_file(filepath, username_var, password_var)
                
                elif file_type == 'custom':
                    content = file_info.get('content', '')
                    self._create_custom_file(filepath, content)
                
                else:
                    print(f"Warning: Unknown file type '{file_type}' for {filepath}")
                    
            except Exception as e:
                print(f"Error creating file {filepath}: {e}")
    
    def validate_environment(self) -> List[str]:
        """Validate that all required environment variables are present."""
        missing_vars = []
        files_config = self.config.get('files', {})
        
        for file_info in files_config:
            file_type = file_info.get('type')
            
            if file_type == 'env':
                variables = file_info.get('variables', [])
                
                if isinstance(variables, list):
                    # Old format: list of variable names
                    for var in variables:
                        if var not in self.env_vars:
                            missing_vars.append(var)
                elif isinstance(variables, dict):
                    # New format: check source variables
                    for output_var, source_config in variables.items():
                        if isinstance(source_config, str):
                            if source_config not in self.env_vars:
                                missing_vars.append(source_config)
                        elif isinstance(source_config, dict):
                            required_vars = source_config.get("vars", [])
                            for var in required_vars:
                                if var not in self.env_vars:
                                    missing_vars.append(var)
            
            elif file_type == 'db':
                username_var = file_info.get('username_var')
                password_var = file_info.get('password_var')
                if username_var and username_var not in self.env_vars:
                    missing_vars.append(username_var)
                if password_var and password_var not in self.env_vars:
                    missing_vars.append(password_var)
        
        return list(set(missing_vars))  # Remove duplicates


def main():
    parser = argparse.ArgumentParser(description='Generate configuration files from environment variables')
    parser.add_argument('config', help='JSON configuration file (envs_dev.json or envs_prod.json)')
    parser.add_argument('--env-file', '-e', default='secrets.env', help='Environment file to load variables from (default: secrets.env)')
    parser.add_argument('--validate', action='store_true', help='Only validate environment variables')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be created without creating files')
    
    args = parser.parse_args()
    
    try:
        generator = ConfigGenerator(args.config, args.env_file)
        
        if args.validate:
            missing_vars = generator.validate_environment()
            if missing_vars:
                print("Missing environment variables:")
                for var in missing_vars:
                    print(f"  - {var}")
                return 1
            else:
                print("All required environment variables are present")
                return 0
        
        if args.dry_run:
            print("Dry run - files that would be created:")
            files_config = generator.config.get('files', {})
            for file_info in files_config:
                print(f"  - {file_info.get('type')}: {file_info.get('path')}")
            return 0
        
        generator.generate_files()
        print("Configuration files generated successfully")
        
    except Exception as e:
        print(f"Error: {e}")
        return 1
    
    return 0


if __name__ == '__main__':
    exit(main())